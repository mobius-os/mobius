import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import Drawer from '../Drawer/Drawer.jsx'
import AppCanvas from '../AppCanvas/AppCanvas.jsx'
import ChatView from '../ChatView/ChatView.jsx'
import ErrorBoundary from '../ErrorBoundary/ErrorBoundary.jsx'
import SettingsView from '../SettingsView/SettingsView.jsx'
import WalkthroughOverlay from '../Walkthrough/WalkthroughOverlay.jsx'
import { api, BASE } from '../../api/client.js'
import usePushSubscription from '../../hooks/usePushSubscription.js'
import useNavigation from '../../hooks/useNavigation.js'
import useSystemEventStream from '../../hooks/useSystemEventStream.js'
import useTheme from '../../hooks/useTheme.js'
import useProviderAuthStatus from '../../hooks/useProviderAuthStatus.js'
import useOnlineStatus from '../../hooks/useOnlineStatus.js'
import { appQueries, chatQueries, modelQueries, ownerQueries } from '../../hooks/queries.js'
import { appVersionKey } from '../../lib/appVersion.js'
import './Shell.css'

export default function Shell() {
  const {
    activeView, setActiveView,
    activeAppId, setActiveAppId,
    activeChatId, setActiveChatId,
    drawerOpen, openDrawer, closeDrawer,
    navTo, backFiredRef, drawerPushedRef, navStackRef,
    activeViewRef, activeChatIdRef, activeAppIdRef,
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
  const [appCache, setAppCache] = useState([])
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
  const [toast, setToast] = useState(null)
  // Global connectivity indicator. The composer already disables send when
  // offline (ChatView); this surfaces the state shell-wide so the user is
  // never tapping in the dark about whether they're connected.
  const online = useOnlineStatus()
  const chatsLoadedRef = useRef(false)
  // In-flight guard for newChat. The function POSTs unconditionally now
  // (the old empty-chat-reuse path was the implicit deduper); without
  // this guard a rapid double-tap on "+ New chat" before the API
  // returns races two creates and leaves an extra empty chat behind.
  const creatingChatRef = useRef(false)
  const [builtApp, setBuiltApp] = useState(null)
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
    const stale = appCache.filter(
      id => !liveIds.has(id) && seenAppIdsRef.current.has(id)
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
      if (!liveFetched && !chatsQuery.isFetching) refreshChats()
    } else if (prev && !liveFetched) {
      // Persisted snapshot is missing `prev` but we haven't heard
      // from the server yet. Hold `prev` as a tentative restore —
      // ChatView mounts on it, and if it's gone server-side, the
      // 404 from ChatView's own fetch surfaces a retryable error
      // instead of a silent chat-switch. Nudge the chats query in
      // case TanStack's staleTime (30s in queryClient.js) skipped
      // the on-mount refetch — without that nudge a fresh persisted
      // snapshot pins us here indefinitely.
      if (!chatsQuery.isFetching) refreshChats()
    } else {
      // `prev` is null (cold cache, deep-link, etc.), or live data
      // confirmed `prev` is gone. Demote to the most recent chat,
      // or null when there genuinely are none (the bootstrap effect
      // below handles that case).
      setActiveChatId(chats[0]?.id || null)
    }
    chatsLoadedRef.current = true
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
      refreshApps()
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
        refreshApps().then(updatedApps => {
          const name = updatedApps.find(a => String(a.id) === String(ev.appId))?.name || null
          setBuiltApp({ id: Number(ev.appId), name })
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
    } else if (ev.type === 'shell_rebuilt') {
      // Deduplicate against the SSE catch-up burst to avoid reload loops.
      const now = Date.now()
      const lastRebuilt = Number(sessionStorage.getItem('shell-rebuilt-at') || 0)
      if (now - lastRebuilt < 5000) return
      sessionStorage.setItem('shell-rebuilt-at', String(now))
      sessionStorage.setItem('shell-reload', JSON.stringify({
        activeView,
        activeAppId,
        drawerOpen,
        activeChatId,
      }))
      // Match the manifest scope so the post-reload page lands inside
      // the installed PWA's declared scope — writing `/` here would
      // briefly put the page out of scope and Chromium can refuse the
      // next manifest update in-place.
      window.history.replaceState(null, '', '/shell/')
      document.body.style.transition = 'opacity 0.2s ease'
      document.body.style.opacity = '0'
      setTimeout(() => window.location.reload(), 220)
    } else if (ev.type === 'shell_rebuild_failed') {
      setToast('Shell rebuild failed.')
      setTimeout(() => setToast(null), 8000)
    }
  }, [
    activeAppId, activeView, drawerOpen, activeChatId,
    activeChatIdRef, loadTheme, markStreamingEnd, markStreamingStart,
    refreshApps, refreshChats,
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
  //   moebius:open-chat — open an existing chat, optionally pre-filling a draft.
  //   moebius:open-app — switch the shell to an installed app. Payload
  //     {appId} accepts either the numeric DB id or the slug; we match
  //     against the installed apps list and silently ignore unknown ids
  //     (don't crash the shell on a stale or malicious payload). Mirrors
  //     the drawer's onApp wiring (navTo('canvas', { appId })) so the
  //     existing iframe LRU + back-stack behavior applies.
  useEffect(() => {
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
        try { sessionStorage.setItem('pending-draft', report) } catch {}
        // Set view and chatId together to avoid flashing the previous chat.
        setActiveView('chat')
        setActiveChatId(buildingChatId)
        refreshChats()
      } else {
        newChat({ draft: report, forceNew: true })
      }
    }

    async function onMessage(e) {
      // window 'message' events are for cross-frame postMessage —
      // mini-app iframes (origin 'null' from sandboxed iframes) or
      // same-origin sibling frames. NOT service-worker messages —
      // those arrive on navigator.serviceWorker, handled separately
      // below.
      if (e.origin !== 'null' && e.origin !== window.location.origin) return
      if (e.data?.type === 'moebius:app-error') {
        handleAppError(e)
      } else if (e.data?.type === 'moebius:new-chat') {
        newChat({ draft: e.data.draft, forceNew: true })
      } else if (e.data?.type === 'moebius:open-chat') {
        if (typeof e.data.chatId !== 'string' || !e.data.chatId) return
        if (e.data.draft) {
          try { sessionStorage.setItem('pending-draft', String(e.data.draft)) } catch {}
        }
        navTo('chat', { chatId: e.data.chatId })
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
          setToast('App is not installed yet.')
          setTimeout(() => setToast(null), 6000)
          return
        }
        navTo('canvas', { appId: app.id })
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
        return
      }
      // Legacy out-of-scope forms, still accepted.
      const appMatch = path.match(/^\/app\/([^/]+)$/)
      const chatMatch = path.match(/^\/chat\/([^/]+)$/)
      if (appMatch) {
        navTo('canvas', { appId: parseInt(appMatch[1], 10) })
      } else if (chatMatch) {
        navTo('chat', { chatId: chatMatch[1] })
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
  }, [apps, chats, navTo, refreshApps])

  async function newChat({ draft, forceNew, exclude } = {}) {
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
        && (!draft || String(c.id) !== String(activeChatIdRef.current)))
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
        setToast('You’re offline.')
        setTimeout(() => setToast(null), 4000)
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
        // Don't leave a dead, drawer-still-open tap on a failed create.
        setToast('Couldn’t start a new chat — please try again.')
        setTimeout(() => setToast(null), 4000)
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
      if (!drawerPushedRef.current) history.pushState(null, '')
      drawerPushedRef.current = false
      navStackRef.current.push({
        view: activeViewRef.current,
        chatId: activeChatIdRef.current,
        appId: activeAppIdRef.current,
      })
    }
    closeDrawer()
    if (draft) {
      try { sessionStorage.setItem('pending-draft', draft) } catch {}
    }
    setActiveView('chat')
    setActiveChatId(chatId)
  }

  function selectChat(id) {
    clearChatAttention(id)
    navTo('chat', { chatId: id })
    setBuiltApp(null)
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
      return
    }
    if (!res.ok) {
      if (res.status === 409) {
        // TODO: surface a toast once we have that primitive. For now
        // the chat row stays in the list and the user can retry.
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
    <div className="shell">
      <header className="shell__bar">
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
        onNewChat={newChat}
        onDeleteChat={deleteChat}
        onSettings={() => navTo('settings')}
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

      <main className="shell__content">
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
            onFirstMessage={refreshChats}
            onSystemEvent={handleSystemEvent}
            builtApp={builtApp}
            onOpenApp={(appId) => { navTo('canvas', { appId }); setBuiltApp(null) }}
            onMessageStart={() => {
              // User just sent a message — the agent is about to
              // stream a response. Mark this chat as streaming so the
              // drawer's pulse dot picks it up immediately (no
              // round-trip wait for the first SSE event).
              markStreamingStart(activeChatId)
              setBuiltApp(null)
            }}
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
              onNavPush={appNavPush}
              onNavPop={appNavPop}
              onNavReset={appNavReset}
            />
          </div>
        ))}
        {activeView === 'settings' && (
          <SettingsView onThemeChange={loadTheme} />
        )}
      </main>
      {toast && (
        <div
          style={{
            position: 'fixed',
            bottom: '1rem',
            left: '50%',
            transform: 'translateX(-50%)',
            background: 'var(--danger, #ef4444)',
            color: '#fff',
            padding: '0.75rem 1.5rem',
            borderRadius: '0.5rem',
            fontSize: '0.875rem',
            zIndex: 9000,
            maxWidth: '90vw',
            textAlign: 'center',
          }}
        >
          {toast}
        </div>
      )}
    </div>
  )
}
