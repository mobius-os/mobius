import { useState, useEffect, useCallback, useMemo, useReducer, useRef } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Minimize2 } from 'lucide-react'
import Drawer from '../Drawer/Drawer.jsx'
import Toast from '../ui/Toast.jsx'
import AppCanvas from '../AppCanvas/AppCanvas.jsx'
import ChatView from '../ChatView/ChatView.jsx'
import ErrorBoundary from '../ErrorBoundary/ErrorBoundary.jsx'
import SettingsView from '../SettingsView/SettingsView.jsx'
import WalkthroughOverlay from '../Walkthrough/WalkthroughOverlay.jsx'
import { api, apiFetch, BASE } from '../../api/client.js'
import usePushSubscription from '../../hooks/usePushSubscription.js'
import useNavigation, { coldRestoredCanvasAppId } from '../../hooks/useNavigation.js'
import { pushNavEntry, replaceNavEntry } from '../../lib/navHistory.js'
import useSystemEventStream from '../../hooks/useSystemEventStream.js'
import useTheme from '../../hooks/useTheme.js'
import useProviderAuthStatus from '../../hooks/useProviderAuthStatus.js'
import useOnlineStatus from '../../hooks/useOnlineStatus.js'
import { appQueries, chatQueries, modelQueries, ownerQueries } from '../../hooks/queries.js'
import { appVersionKey } from '../../lib/appVersion.js'
import { immersiveReducer, isImmersiveActive } from '../../lib/immersive.js'
import {
  shellRebuildFailureDetails,
  shellRebuildFailureMessage,
} from '../../lib/shellRebuildFailure.js'
import {
  APP_LRU_STORAGE_KEY, mergeAppLru, parseStoredAppLru, selectAppsToWarm,
} from '../../lib/appPrecache.js'
import {
  builtAppForChat,
  withBuiltAppForChat,
  withoutBuiltAppForChat,
} from './builtAppState.js'
import { shouldDeferShellReload } from './shellReloadPolicy.js'
import './Shell.css'

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

const SHELL_BUILDING_NOTICE_DELAY_MS = 1200
const SHELL_FAILURE_NOTICE_DELAY_MS = 1200
const SHELL_FAILURE_AUTO_DISMISS_MS = 6000
const SHELL_RELOAD_RECHECK_MS = 6000

export default function Shell() {
  const {
    activeView, setActiveView,
    activeAppId, setActiveAppId,
    activeChatId, setActiveChatId,
    drawerOpen, openDrawer, closeDrawer,
    navTo, backFiredRef, drawerPushedRef, navStackRef,
    activeViewRef, activeChatIdRef, activeAppIdRef,
    drawerOpenRef,
    appNavPush, appNavPop, appNavReset,
  } = useNavigation()

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
  // LRU cache of recently-visited app IDs (most-recent first).
  // Each entry stays mounted as a hidden iframe so re-opening it via
  // drawer-tap or back-nav is instant — no module re-fetch, no
  // WebGL re-init, no app-side data refetch. Bounded by APP_CACHE_MAX
  // to keep memory predictable on phones (each Three.js / WebGL app
  // can hold tens of MB).
  const APP_CACHE_MAX = 4
  const [appCache, setAppCache] = useState(
    () => (coldRestoredCanvasAppId != null ? [coldRestoredCanvasAppId] : [])
  )
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
  function copyShellRebuildFailure(details) {
    if (!details || typeof navigator === 'undefined' || !navigator.clipboard) {
      showToast('Build details are in the server logs.', { duration: 5000 })
      return
    }
    navigator.clipboard.writeText(details)
      .then(() => showToast('Build details copied.'))
      .catch(() => showToast('Could not copy build details.', { variant: 'error' }))
  }
  const handleAppIntentDelivered = useCallback((appId, delivered) => {
    setAppIntents((prev) => {
      const key = String(appId)
      if (!prev[key] || prev[key].nonce !== delivered?.nonce) return prev
      const next = { ...prev }
      delete next[key]
      return next
    })
  }, [])

  // Shell update lifecycle shown in the top bar. This is deliberately separate
  // from transient toasts: owner-initiated shell work should be visible while it
  // is happening, and a ready update should not steal focus just to prove it
  // landed.
  const [shellUpdate, setShellUpdate] = useState({ state: 'idle' })
  const shellUpdateRef = useRef(shellUpdate)
  useEffect(() => { shellUpdateRef.current = shellUpdate }, [shellUpdate])


  const pendingShellReloadRef = useRef(false)
  const shellReloadTimerRef = useRef(null)
  const shellBuildingNoticeTimerRef = useRef(null)
  const shellFailureNoticeTimerRef = useRef(null)
  const shellFailureDismissTimerRef = useRef(null)
  const lastShellInteractionAtRef = useRef(0)
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
    return {
      activeView: activeViewRef.current,
      activeAppId: activeAppIdRef.current,
      drawerOpen: drawerOpenRef.current,
      activeChatId: activeChatIdRef.current,
    }
  }

  function setShellUpdateState(next) {
    setShellUpdate(prev => (prev.state === next.state ? prev : next))
  }

  function clearShellUpdateTimers() {
    if (shellBuildingNoticeTimerRef.current) {
      clearTimeout(shellBuildingNoticeTimerRef.current)
      shellBuildingNoticeTimerRef.current = null
    }
    if (shellFailureNoticeTimerRef.current) {
      clearTimeout(shellFailureNoticeTimerRef.current)
      shellFailureNoticeTimerRef.current = null
    }
    if (shellFailureDismissTimerRef.current) {
      clearTimeout(shellFailureDismissTimerRef.current)
      shellFailureDismissTimerRef.current = null
    }
  }

  function performShellReload() {
    pendingShellReloadRef.current = false
    if (shellReloadTimerRef.current) {
      clearTimeout(shellReloadTimerRef.current)
      shellReloadTimerRef.current = null
    }
    clearShellUpdateTimers()
    setShellUpdateState({ state: 'refreshing' })
    sessionStorage.setItem('shell-reload', JSON.stringify(shellReloadState()))
    // Match the manifest scope so the post-reload page lands inside
    // the installed PWA's declared scope — writing `/` here would
    // briefly put the page out of scope and Chromium can refuse the
    // next manifest update in-place.
    replaceNavEntry('base', '/shell/')
    setTimeout(() => window.location.reload(), 180)
  }

  function shellReloadWouldDisruptUser() {
    return shouldDeferShellReload({
      activeElement: document.activeElement,
      activeView: activeViewRef.current,
      activeChatId: activeChatIdRef.current,
      streamingChatIds: streamingChatIdsRef.current,
      lastUserInteractionAt: lastShellInteractionAtRef.current,
      visibilityState: document.visibilityState,
    })
  }

  function scheduleShellReloadCheck() {
    if (shellReloadTimerRef.current) clearTimeout(shellReloadTimerRef.current)
    shellReloadTimerRef.current = setTimeout(() => {
      shellReloadTimerRef.current = null
      if (!pendingShellReloadRef.current) return
      if (shellReloadWouldDisruptUser()) {
        scheduleShellReloadCheck()
      } else {
        performShellReload()
      }
    }, SHELL_RELOAD_RECHECK_MS)
  }

  function deferShellReload() {
    pendingShellReloadRef.current = true
    clearShellUpdateTimers()
    // Keep an existing ready badge steady instead of re-announcing every
    // watcher rebuild. A sibling agent may save several shell files in one
    // task; the owner only needs one persistent "refresh when ready" affordance.
    setShellUpdateState({ state: 'ready' })
    scheduleShellReloadCheck()
  }

  function requestShellReload() {
    if (shellReloadWouldDisruptUser()) {
      deferShellReload()
    } else {
      performShellReload()
    }
  }

  function noteShellRebuilding() {
    // If a refresh is already pending, do not flip the badge back to
    // "Updating…" on every subsequent source save. The ready affordance is the
    // useful state now: it tells the owner a refresh will pick up the latest
    // completed build when they are ready.
    if (pendingShellReloadRef.current || shellUpdateRef.current.state === 'ready') return
    clearShellUpdateTimers()
    shellBuildingNoticeTimerRef.current = setTimeout(() => {
      shellBuildingNoticeTimerRef.current = null
      if (pendingShellReloadRef.current || shellUpdateRef.current.state === 'ready') return
      setShellUpdateState({ state: 'building' })
    }, SHELL_BUILDING_NOTICE_DELAY_MS)
  }

  function noteShellRebuildFailed(event) {
    if (pendingShellReloadRef.current || shellUpdateRef.current.state === 'ready') return
    const details = shellRebuildFailureDetails(event)
    const message = shellRebuildFailureMessage(event)
    clearShellUpdateTimers()
    // The watcher can observe an in-progress file write and publish a failure
    // that is immediately followed by a successful rebuild. Give success a
    // chance to arrive before showing an error, and auto-clear the error if it
    // really does remain visible.
    shellFailureNoticeTimerRef.current = setTimeout(() => {
      shellFailureNoticeTimerRef.current = null
      setShellUpdateState({ state: 'failed', message, details })
      shellFailureDismissTimerRef.current = setTimeout(() => {
        shellFailureDismissTimerRef.current = null
        if (shellUpdateRef.current.state === 'failed') {
          setShellUpdateState({ state: 'idle' })
        }
      }, SHELL_FAILURE_AUTO_DISMISS_MS)
    }, SHELL_FAILURE_NOTICE_DELAY_MS)
  }

  useEffect(() => {
    const record = () => { lastShellInteractionAtRef.current = Date.now() }
    const opts = { capture: true, passive: true }
    window.addEventListener('pointerdown', record, opts)
    window.addEventListener('touchstart', record, opts)
    window.addEventListener('keydown', record, opts)
    window.addEventListener('input', record, opts)
    window.addEventListener('focusin', record, opts)
    return () => {
      window.removeEventListener('pointerdown', record, opts)
      window.removeEventListener('touchstart', record, opts)
      window.removeEventListener('keydown', record, opts)
      window.removeEventListener('input', record, opts)
      window.removeEventListener('focusin', record, opts)
      if (shellReloadTimerRef.current) clearTimeout(shellReloadTimerRef.current)
      clearShellUpdateTimers()
    }
  }, [])

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
  const [builtAppsByChatId, setBuiltAppsByChatId] = useState({})
  const builtApp = builtAppForChat(builtAppsByChatId, activeChatId)
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

  const clearChatAttention = useCallback((chatId) => {
    if (!chatId) return
    setAttentionChatIds(prev => {
      if (!prev.has(chatId)) return prev
      const next = new Set(prev)
      next.delete(chatId)
      return next
    })
  }, [])

  useEffect(() => {
    if (activeView === 'chat') clearChatAttention(activeChatId)
  }, [activeChatId, activeView, clearChatAttention])

  // Immersive mode (moebius:immersive, .pm/128). The state is the id of
  // the app holding an immersive request (or null); it's APPLIED — bar
  // hidden, canvas full-viewport — only while that app is the active
  // canvas, so switching to chat/settings/another app restores chrome
  // automatically and switching back re-enters without a re-post. The
  // request reaches us through AppCanvas, which verifies the message's
  // event.source against its own iframe before forwarding — the ACTIVE-
  // iframe-only guarantee lives there. Full contract: lib/immersive.js.
  const [immersiveAppId, dispatchImmersive] = useReducer(immersiveReducer, null)
  // Stable identity — AppCanvas's message-listener effect depends on it.
  const handleImmersive = useCallback((appId, value) => {
    dispatchImmersive({ type: 'request', appId, value })
  }, [])
  const immersiveActive = isImmersiveActive(immersiveAppId, activeView, activeAppId)

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

  // Maintain the LRU: when activeAppId changes, move it to the front
  // of the cache (mounting it if new). Caps at APP_CACHE_MAX; the
  // tail is evicted (its iframe unmounts, freeing memory).
  useEffect(() => {
    if (activeAppId === null || activeAppId === undefined) return
    setAppCache(prev => {
      const filtered = prev.filter(id => id !== activeAppId)
      return [activeAppId, ...filtered].slice(0, APP_CACHE_MAX)
    })
  }, [activeAppId])

  // Cross-session recency for SW cache warming. The persisted LRU read
  // once at mount (useState initializer, so the persist effect below
  // can't clobber it first) feeds the warm-on-load effect; every LRU
  // rotation then MERGES into storage rather than overwriting, keeping
  // depth WARM_APP_LIMIT across sessions (the in-memory list holds only
  // APP_CACHE_MAX ids). Failures degrade to pinned-only warming.
  const [initialAppLru] = useState(() => {
    try {
      return parseStoredAppLru(localStorage.getItem(APP_LRU_STORAGE_KEY))
    } catch { return [] }
  })
  useEffect(() => {
    // The empty mount state carries no recency information — persisting
    // it would erase the previous session's signal before it's used.
    if (appCache.length === 0) return
    try {
      const stored = parseStoredAppLru(localStorage.getItem(APP_LRU_STORAGE_KEY))
      localStorage.setItem(
        APP_LRU_STORAGE_KEY, JSON.stringify(mergeAppLru(appCache, stored)),
      )
    } catch { /* storage unavailable (private mode) — warming degrades */ }
  }, [appCache])

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

  // Evict tombstoned apps from the LRU. When an app is uninstalled
  // (feature 110 soft-delete) it drops out of /api/apps, the server
  // 404s its /module + /frame, and `app_updated` fires → refreshApps.
  // But the hidden AppCanvas iframe stays mounted as long as its id is
  // in appCache, so it re-fetches /module, 404s, and shows an error
  // canvas that lingers until the next navigation. Reconcile the cache
  // against the live apps list: any cached id no longer present is
  // dropped so its AppCanvas unmounts. If the tombstoned app is the
  // ACTIVE canvas, fall back to the chat view so the user isn't left
  // staring at the error canvas (feature 114).
  //
  // Gate on a live-confirmed list (isSuccess + isFetchedAfterMount),
  // mirroring the chat-demotion effect above: a transiently-empty
  // `apps` (cold cache, a refetch that resolved to []) must not evict
  // valid cached apps. This is bookkeeping only — render still iterates
  // the id-sorted snapshot, never appCache's LRU order directly.
  const appsLiveFetched = appsQuery.isSuccess && appsQuery.isFetchedAfterMount
  useEffect(() => {
    if (!appsLiveFetched) return
    const liveIds = new Set(apps.map(a => a.id))
    // Record everything the live list currently shows, so a later
    // disappearance reads as a real uninstall rather than a never-seen id.
    for (const id of liveIds) seenAppIdsRef.current.add(id)
    if (appCache.length === 0) return
    // Evict only ids that were once present and are now gone — a genuine
    // present→absent transition. A cached id we've never seen in a fetched
    // list yet (just-opened app whose query hasn't caught up) is left alone
    // until the list confirms it; this is the open-app stale-list race guard.
    //
    // Never evict an app that a back-stack entry still points at. `/api/apps/`
    // is NetworkFirst, so a drawer-open refetch can resolve to a stale
    // cache-fallback list that transiently omits a still-installed app; if
    // that app is a back target, evicting it here scrubs it from navStackRef
    // and the back-gesture skips it (A→B→C then back landed on A, not B).
    // A genuine LOCAL uninstall scrubs the nav-stack via deleteApp, so this
    // generic list-reconciliation must not.
    const navHeldAppIds = new Set(
      navStackRef.current
        .filter(e => e.view === 'canvas' && e.appId != null)
        .map(e => e.appId)
    )
    // The ACTIVE canvas is never exempt: an app deleted out-of-band while it is
    // the current view must still be evicted so the shell falls back to chat
    // (feature 114), even when an earlier visit also left it on the back-stack
    // (chat→A→B→A leaves A both active AND a back target). Only genuine back
    // targets get the stale-list protection.
    if (activeView === 'canvas' && activeAppId != null) {
      navHeldAppIds.delete(activeAppId)
    }
    const stale = appCache.filter(
      id => !navHeldAppIds.has(id)
        && !liveIds.has(id)
        && seenAppIdsRef.current.has(id)
    )
    if (stale.length === 0) return
    // Evict exactly the confirmed-stale ids — NOT every id absent from
    // liveIds. A just-opened app we've not yet seen present is absent from
    // liveIds for one render but must survive; filtering on `stale` (which
    // already excludes never-seen ids) keeps it mounted.
    const staleSet = new Set(stale)
    setAppCache(prev => prev.filter(id => !staleSet.has(id)))
    // Drop any back-stack entries pointing at a now-dead app so a later
    // back-gesture can't restore the canvas of an uninstalled app (which
    // would render a blank `<main>` — its iframe is evicted). Mirrors the
    // navStack scrub deleteChat does for deleted chats.
    navStackRef.current = navStackRef.current.filter(
      e => !(e.view === 'canvas' && stale.includes(e.appId))
    )
    // If we're sitting ON the tombstoned app, leave the canvas for the
    // chat view directly (not navTo — there's no meaningful "back to the
    // app" target to push; the app is gone). setActiveView last so the
    // canvas->chat flip only happens once activeView's payload is set.
    if (activeView === 'canvas' && stale.includes(activeAppId)) {
      setActiveAppId(null)
      setActiveView('chat')
    }
  }, [apps, appsLiveFetched, appCache, activeView, activeAppId,
      navStackRef, setActiveAppId, setActiveView])

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
    // Restored app is gone (uninstalled since): evict the seeded iframe so
    // it can't sit stuck-mounted (the present->absent eviction above never
    // fires for an id that was never seen present this session), and demote
    // the canvas to chat if we're sitting on it.
    setAppCache(prev => prev.filter(id => id !== coldRestoredCanvasAppId))
    if (activeView === 'canvas'
        && Number(activeAppId) === Number(coldRestoredCanvasAppId)) {
      setActiveAppId(null)
      setActiveView('chat')
    }
  }, [appsLiveFetched, apps, activeView, activeAppId, setActiveAppId, setActiveView])

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
      // No restored chat target exists yet. Demote immediately to the
      // newest listed chat, or let the bootstrap effect create one if the
      // owner has no listed chats.
      setActiveChatId(chats[0]?.id || null)
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
          setActiveChatId(chats[0]?.id || null)
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
      refreshChats, setActiveChatId, activeChatIdRef])

  useEffect(() => { if (drawerOpen) { refreshApps(); refreshChats() } }, [drawerOpen, refreshApps, refreshChats])

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
    } else if (ev.type === 'app_updated') {
      // LIST-REFRESH ONLY. Refetch the apps list so the affected app's
      // `updated_at` reflects the server's new state. versionForApp reads
      // from that field, so the iframe URL automatically picks up the new
      // cache-buster on the next render — no separate version counter to
      // keep in sync. This event reaches every view (it's on the global
      // SystemBroadcast), so it must NOT plant the "Open app" CTA — that's
      // the chat-scoped `app_built` event's job (below). Doing the CTA here
      // is what leaked it into unrelated chats.
      refreshApps().then(updatedApps => {
        // Warm the SW cache for the updated app immediately — the edit
        // rotated the `?v=` cache key, so without this the next open pays
        // the network round trip. Every app's read path is cached now
        // (not just offline-capable ones), so no flag gate here.
        if (ev.appId) {
          const app = updatedApps.find(a => String(a.id) === String(ev.appId))
          if (app) warmAppCode(app)
        }
      })
    } else if (ev.type === 'app_built') {
      // CHAT-SCOPED CTA. The backend publishes `app_built` onto ONLY the
      // broadcast of the chat that built the app (routes/notify.py), so it
      // arrives exclusively via that chat's own SSE stream — ChatView
      // forwards it here through onSystemEvent. Because the event never
      // touches the global SystemBroadcast or any other chat's stream, the
      // CTA is naturally scoped to the building chat and cannot leak. Still
      // refresh the apps list so the name lookup below resolves the fresh
      // row (app_built and app_updated arrive close together but order
      // isn't guaranteed).
      if (ev.appId) {
        const sourceChatId = ev.chatId || activeChatIdRef.current
        refreshApps().then(updatedApps => {
          const app = updatedApps.find(a => String(a.id) === String(ev.appId))
          const name = app?.name || null
          setBuiltAppsByChatId(prev => withBuiltAppForChat(
            prev,
            sourceChatId,
            { id: Number(ev.appId), name },
          ))
          // Warm the SW cache immediately after a build lands so the
          // "Open app" CTA tap is served from cache.
          if (app) warmAppCode(app)
        })
      }
    } else if (ev.type === 'chat_run_started') {
      if (ev.chatId) markStreamingStart(ev.chatId)
      refreshChats()
    } else if (ev.type === 'chat_run_finished') {
      const chatId = ev.chatId
      if (chatId) {
        markStreamingEnd(chatId)
        if (
          activeViewRef.current !== 'chat'
          || String(chatId) !== String(activeChatIdRef.current || '')
        ) {
          setAttentionChatIds(prev => {
            if (prev.has(chatId)) return prev
            const next = new Set(prev)
            next.add(chatId)
            return next
          })
        }
      }
      refreshChats()
    } else if (ev.type === 'shell_rebuilding') {
      noteShellRebuilding()
    } else if (ev.type === 'shell_rebuilt') {
      clearShellUpdateTimers()
      // Deduplicate against the SSE catch-up burst to avoid reload loops.
      const now = Date.now()
      const lastRebuilt = Number(sessionStorage.getItem('shell-rebuilt-at') || 0)
      if (now - lastRebuilt < 5000) return
      sessionStorage.setItem('shell-rebuilt-at', String(now))
      // Read view state from refs, not closure-captured state. The
      // callback's dependency array includes the scalar state values
      // (activeView, activeAppId, etc.) because React requires them for
      // correctness checking, but by the time shell_rebuilt fires those
      // closure values may be one render behind concurrent state updates.
      // Sibling branches (chat_run_started, chat_run_finished) already
      // use refs for exactly this reason. If the owner is typing, steering,
      // or reading the currently-running chat, hold the refresh behind a
      // steady top-bar badge instead of fading the whole shell black and
      // stealing focus.
      requestShellReload()
    } else if (ev.type === 'shell_rebuild_failed') {
      noteShellRebuildFailed(ev)
    }
  }, [
    // Scalar state removed: shell_rebuilt now reads from refs (activeViewRef,
    // activeAppIdRef, activeChatIdRef, drawerOpenRef) so stale closure values
    // can't be serialized. Refs themselves don't need to be in deps (they're
    // stable objects whose .current is read at call time, not at capture time).
    loadTheme, markStreamingEnd, markStreamingStart,
    refreshApps, refreshChats, warmAppCode,
  ])

  // Shell-level SSE subscription for system events. Stays open for
  // the lifetime of the Shell so theme/app/shell-rebuild updates
  // reach handleSystemEvent regardless of which view the user is on.
  // The active chat's SSE stream still forwards the same events for
  // in-chat catch-up coherence — handlers are idempotent (theme
  // reload, refreshApps, version bump) so the duplicate is harmless.
  useSystemEventStream(handleSystemEvent)

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
      'image-generation',
      'models',
    ])
    const findAppForOpenTarget = (list, target) => {
      if (target == null) return null
      return (list || []).find(a =>
        String(a.id) === String(target) || a.slug === target) || null
    }

    async function handleAppError(e) {
      const appEntry = apps.find(a => String(a.id) === String(e.data.appId))
      const appName = appEntry?.name || `app ${e.data.appId}`
      const report = `The app "${appName}" crashed with this error:\n\`\`\`\n${e.data.error}\n\`\`\`\nPlease investigate and fix.`

      const buildingChatId = appEntry?.chat_id || e.data.chatId || null
      const buildingChat = buildingChatId && chats.find(c => c.id === buildingChatId)
      if (buildingChat) {
        try {
          sessionStorage.setItem('pending-draft', report)
          sessionStorage.setItem(`draft:${buildingChatId}`, report)
          sessionStorage.removeItem('pending-draft-autosend')
          sessionStorage.removeItem(`draft-autosend:${buildingChatId}`)
        } catch {}
        // Set view and chatId together to avoid flashing the previous chat.
        setActiveView('chat')
        setActiveChatId(buildingChatId)
        refreshChats()
      } else {
        newChat({ draft: report, forceNew: true })
      }
    }

    async function onMessage(e) {
      // window 'message' events are for cross-frame postMessage from
      // same-origin sibling frames. NOT service-worker messages —
      // those arrive on navigator.serviceWorker, handled separately
      // below.
      //
      // The iframes mount with allow-same-origin so e.origin is always
      // window.location.origin, never the string 'null'. The 'null'-origin
      // branch was dead (sandboxed-without-allow-same-origin iframes).
      // e.source is intentionally NOT checked: Möbius is single-owner and
      // every iframe on this origin is owned by the shell, so source
      // verification would add complexity with no security benefit.
      if (e.origin !== window.location.origin) return
      if (e.data?.type === 'moebius:app-error') {
        handleAppError(e)
      } else if (e.data?.type === 'moebius:new-chat') {
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
        const target = e.data.appId
        let app = findAppForOpenTarget(apps, target)
        if (!app) {
          const updatedApps = await refreshApps()
          app = findAppForOpenTarget(updatedApps, target)
        }
        if (!app) {
          showToast('App is not installed yet.', { duration: 6000 })
          return
        }
        const intent = typeof e.data.intent === 'string' ? e.data.intent.trim() : ''
        if (intent) {
          setAppIntents((prev) => ({
            ...prev,
            [String(app.id)]: { intent, nonce: Date.now() },
          }))
        }
        navTo('canvas', { appId: app.id })
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
        let app = null, chat = null
        try {
          const params = new URLSearchParams(search)
          app = params.get('app')
          chat = params.get('chat')
        } catch { /* no query */ }
        if (app) navTo('canvas', { appId: parseInt(app, 10) })
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
  }, [apps, chats, navTo, refreshApps, refreshChats])

  async function newChat({ draft, forceNew, exclude, autoSend, focusComposer } = {}) {
    // Reuse the most-recently-updated empty chat if one exists; only
    // POST a fresh row when no empty is available. Safe to reuse now
    // that create_chat leaves agent_settings_json NULL — an untouched
    // empty reads the live global default from agent-settings.json on
    // render, so the user always sees their most recent model/effort
    // pick. (Before, create_chat snapshotted defaults at creation time
    // and reuse surfaced that stale snapshot, which is what made the
    // empty-chat reuse path buggy in the first place.)
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
    // Reuse the most-recently-updated empty chat if one exists — INCLUDING
    // the one we're already on. An earlier fix excluded the active chat (to
    // avoid a no-op setActiveChatId), but that backfired: a tap on a blank
    // chat then spawned a SECOND blank, or hopped to an identical-looking
    // spare, and repeated taps ping-ponged between indistinguishable empties
    // — which is the "+ New chat does nothing" report. Reusing
    // deterministically (active included) means blanks never accumulate and a
    // tap on a blank simply keeps you on it (the drawer closing is the
    // acknowledgement). One exception: a `draft` must land on a chat that
    // REMOUNTS ChatView to deliver the pending draft, and setActiveChatId to
    // the current id won't remount — so draft calls still skip the active
    // chat. forceNew bypasses reuse entirely.
    //
    // Also exclude any chat that's mid-stream: the cached `has_messages`
    // flag lags the send, so for a beat after the user sends, the chat
    // they just sent to still reads has_messages=false — reusing it would
    // drop them onto a running turn instead of a blank chat (another flavor
    // of "+ New chat did nothing"). streamingChatIds is marked synchronously
    // on message start, so it closes that stale-cache window.
    const empty = !forceNew && [...chats]
      .filter(c => !c.has_messages
        // `exclude` skips a chat the caller knows is invalid — e.g. the
        // just-deleted active chat, still present in `chats` until the
        // post-delete refreshChats lands (else reuse would re-open it).
        && (exclude == null || String(c.id) !== String(exclude))
        && !streamingChatIds.has(c.id)
        && (!draft || String(c.id) !== String(activeChatIdRef.current))
        // Exclude recently-recovered chats: an Undo may restore a chat
        // whose has_messages=true hasn't propagated yet (optimistic delete
        // left the cache with the tombstoned state). Reusing such a chat
        // would silently navigate back into the just-recovered item instead
        // of a genuine empty. The id is cleared once ChatView reports the
        // first message (has_messages is then reliably true).
        && !recoveredChatIdsRef.current.has(c.id))
      .sort((a, b) => (b.updated_at || '').localeCompare(a.updated_at || ''))
      [0]
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

    // Push nav stack so back returns to the previous view (skip
    // automatic calls — bootstrap or chat-deletion-induced re-create).
    // If the drawer was open, its sentinel becomes this nav's back-
    // target (no new pushState needed). Otherwise, push our own
    // sentinel so back returns to the previous view rather than
    // exiting the PWA.
    if (draft || forceNew || drawerPushedRef.current) {
      if (!drawerPushedRef.current) pushNavEntry('nav')
      drawerPushedRef.current = false
      navStackRef.current.push({
        view: activeViewRef.current,
        chatId: activeChatIdRef.current,
        appId: activeAppIdRef.current,
      })
    }
    closeDrawer()
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
    setActiveView('chat')
    setActiveChatId(chatId)
    if (focusComposer) requestComposerFocus(chatId)
  }

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
    if (activeChatId === id) {
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
    // Evict the app from the LRU cache so its iframe unmounts immediately.
    // The eviction effect (appsLiveFetched) would also catch it on the next
    // refreshApps, but evicting inline is faster and avoids a transient
    // "app is gone but iframe is still visible" state.
    setAppCache(prev => prev.filter(cachedId => cachedId !== id))
    navStackRef.current = navStackRef.current.filter(
      e => !(e.view === 'canvas' && e.appId === id)
    )
    if (activeView === 'canvas' && activeAppId === id) {
      setActiveAppId(null)
      setActiveView('chat')
    }
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
        {/* The brand area (logo + wordmark) below is the drawer toggle: it is
            a role=button with aria-label + aria-expanded + Enter/Space
            handling, so it serves touch, pointer, keyboard, and AT alike. A
            standalone visible hamburger was redundant next to it. */}
        <div
          className="shell__brand"
          role="button"
          tabIndex="0"
          aria-label="Toggle navigation"
          aria-expanded={drawerOpen}
          onClick={() => { if (backFiredRef.current) return; drawerOpen ? closeDrawer() : openDrawer() }}
          onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && (drawerOpen ? closeDrawer() : openDrawer())}
        >
          <img className="shell__logo" src={`${BASE}/moebius.png`} alt="" width="30" height="30" />
          <span className="shell__wordmark">Möbius</span>
        </div>
        {shellUpdate.state !== 'idle' && (
          <div
            className={`shell__update shell__update--${shellUpdate.state}`}
            role="status"
            aria-live="polite"
          >
            <span className="shell__update-dot" aria-hidden="true" />
            <span className="shell__update-label">
              {shellUpdate.state === 'building' && 'Updating shell…'}
              {shellUpdate.state === 'ready' && 'Update ready'}
              {shellUpdate.state === 'refreshing' && 'Refreshing…'}
              {shellUpdate.state === 'failed' && (shellUpdate.message || 'Update failed')}
            </span>
            {shellUpdate.state === 'ready' && (
              <button
                type="button"
                className="shell__update-action"
                onClick={performShellReload}
              >
                Refresh
              </button>
            )}
            {shellUpdate.state === 'failed' && (
              <>
                {shellUpdate.details && (
                  <button
                    type="button"
                    className="shell__update-action"
                    onClick={() => copyShellRebuildFailure(shellUpdate.details)}
                  >
                    Copy
                  </button>
                )}
                <button
                  type="button"
                  className="shell__update-action"
                  onClick={() => setShellUpdate({ state: 'idle' })}
                >
                  Dismiss
                </button>
              </>
            )}
          </div>
        )}
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
        settingsWarning={providerAuth.anyDisconnected}
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
      <main className="shell__content" inert={drawerOpen}>
        {/* Single-mount ChatView, keyed by activeChatId. Switching
            chats unmounts and remounts; ChatView's hide-then-reveal
            scroll-restore (visibility:hidden until lazy renderers
            settle) makes that remount visually seamless. We tried
            multi-mount LRU caching to avoid remount entirely, but
            the resulting DOM-reorder on every chat-switch silently
            reset scrollTop after a few rotations. Single-mount with
            hide-then-reveal is structurally simpler and locked in
            by tests. */}
        {activeView === 'chat' && activeChatId && (
          // Guard the chat view: it renders agent-generated markdown
          // (marked + KaTeX/hljs), the likeliest render-crash source. Keyed
          // by activeChatId so switching chats clears a crashed boundary —
          // a broken chat doesn't strand the whole shell.
          <ErrorBoundary key={activeChatId} variant="inline" label="chat">
          <ChatView
            key={activeChatId}
            chatId={activeChatId}
            onStreamEnd={({ continues } = {}) => {
              // ChatView calls this when the agent turn finishes
              // streaming. Keep the running marker across queued
              // continuations; the successor turn owns the next finish.
              if (!continues) markStreamingEnd(activeChatId)
              refreshApps()
              loadTheme()
              refreshChats()
            }}
            onFirstMessage={() => {
              // The chat has its first message — has_messages is now
              // reliably true, so remove it from the recovered-chat guard.
              recoveredChatIdsRef.current.delete(activeChatId)
              refreshChats()
            }}
            onSystemEvent={handleSystemEvent}
            onChatMissing={(missingId) => {
              // ChatView's own load returned a real 404: this chat is gone
              // (deleted out-of-band, or an off-list chat the restore probe had
              // memoized as existing). Drop the stale memo so it can't re-hold
              // it, and demote to a live chat if it's still the active view.
              knownExistingOffListChatIdsRef.current.delete(missingId)
              if (String(activeChatIdRef.current) === String(missingId)) {
                // Read the CURRENT chats (chatsRef), not the possibly-stale
                // `chats` captured in ChatView's load-effect closure — else a
                // late 404 could demote to null instead of the newest chat.
                setActiveChatId(chatsRef.current[0]?.id || null)
              }
            }}
            builtApp={builtApp}
            onOpenApp={(appId) => {
              navTo('canvas', { appId })
              setBuiltAppsByChatId(prev => withoutBuiltAppForChat(prev, activeChatId))
            }}
            onMessageStart={() => {
              // User just sent a message — the agent is about to
              // stream a response. Mark this chat as streaming so the
              // drawer's pulse dot picks it up immediately (no
              // round-trip wait for the first SSE event).
              markStreamingStart(activeChatId)
              setBuiltAppsByChatId(prev => withoutBuiltAppForChat(prev, activeChatId))
            }}
            composerFocusRequest={composerFocusRequest}
            onComposerFocusHandled={handleComposerFocusHandled}
          />
          </ErrorBoundary>
        )}
        {/* Multi-iframe LRU cache: render every recently-visited app
            as its own persisted iframe; only the matching one is
            visible. Re-opening a cached app via drawer-tap or back-
            nav is instant (no iframe reload). Cap is APP_CACHE_MAX.

            Render order is sorted by id (stable across LRU rotations)
            so React never calls insertBefore to reorder the wrappers
            on app-switch. Reordering keyed children causes Chrome to
            reload the sandboxed iframes inside, which then never
            receive a fresh frame-init from the parent and hit the
            10s "Loading timeout" guard. LRU still controls eviction;
            only DOM order is stable. */}
        {[...appCache].sort((a, b) => Number(a) - Number(b)).map(id => (
          <div
            key={id}
            className={`shell__view ${activeView === 'canvas' && activeAppId === id ? 'shell__view--active' : ''}`}
          >
            <AppCanvas
              appId={id}
              version={versionForApp(id)}
              appName={apps.find(a => String(a.id) === String(id))?.name}
              offlineCapable={!!apps.find(a => String(a.id) === String(id))?.offline_capable}
              pendingIntent={appIntents[String(id)] || null}
              // Immersive is APPLIED only while this app is the active holder
              // (immersiveActive already requires the canvas view + active
              // app). Only then does the iframe receive the real safe-area
              // insets to pad under the notch; every other (cached, hidden)
              // iframe gets zeros so it never double-pads behind the chrome.
              immersive={immersiveActive && String(immersiveAppId) === String(id)}
              onNavPush={appNavPush}
              onNavPop={appNavPop}
              onNavReset={appNavReset}
              onImmersive={handleImmersive}
              onIntentDelivered={handleAppIntentDelivered}
            />
          </div>
        ))}
        {activeView === 'settings' && (
          <SettingsView
            onThemeChange={loadTheme}
            onOpenChat={selectChat}
            focusTarget={settingsFocusTarget}
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
      <Toast
        message={toast?.message}
        variant={toast?.variant}
        duration={toast?.duration}
        action={toast?.action}
        onDismiss={dismissToast}
      />
    </div>
  )
}
