import { useState, useEffect, useCallback, useRef } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import Drawer from '../Drawer/Drawer.jsx'
import AppCanvas from '../AppCanvas/AppCanvas.jsx'
import ChatView from '../ChatView/ChatView.jsx'
import SettingsView from '../SettingsView/SettingsView.jsx'
import { api, BASE } from '../../api/client.js'
import usePushSubscription from '../../hooks/usePushSubscription.js'
import useNavigation from '../../hooks/useNavigation.js'
import useSystemEventStream from '../../hooks/useSystemEventStream.js'
import useTheme from '../../hooks/useTheme.js'
import useProviderAuthStatus from '../../hooks/useProviderAuthStatus.js'
import { appQueries, chatQueries } from '../../hooks/queries.js'
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

  // Cache key from app.updated_at (server-side). Stable across reloads.
  const versionForApp = useCallback((id) => {
    const app = apps.find(a => String(a.id) === String(id))
    if (!app?.updated_at) return 0
    const t = Date.parse(app.updated_at)
    return Number.isFinite(t) ? Math.floor(t / 1000) : 0
  }, [apps])
  // LRU cache of recently-visited app IDs (most-recent first).
  // Each entry stays mounted as a hidden iframe so re-opening it via
  // drawer-tap or back-nav is instant — no module re-fetch, no
  // WebGL re-init, no app-side data refetch. Bounded by APP_CACHE_MAX
  // to keep memory predictable on phones (each Three.js / WebGL app
  // can hold tens of MB).
  const APP_CACHE_MAX = 4
  const [appCache, setAppCache] = useState([])
  const [toast, setToast] = useState(null)
  const chatsLoadedRef = useRef(false)
  // In-flight guard for newChat. The function POSTs unconditionally now
  // (the old empty-chat-reuse path was the implicit deduper); without
  // this guard a rapid double-tap on "+ New chat" before the API
  // returns races two creates and leaves an extra empty chat behind.
  const creatingChatRef = useRef(false)
  const [builtApp, setBuiltApp] = useState(null)
  const [pwaPrompt, setPwaPrompt] = useState(null)

  // Set of chat ids whose agent is currently streaming. Used to drive
  // the pulsing dot next to the row in the drawer. ChatView's
  // onStreamStart / onStreamEnd callbacks add and remove entries.
  // Only the active chat's ChatView is mounted at a time, so this is
  // a 0-or-1-element set in practice; switching chats while a turn is
  // in flight removes the previous chat's id (ChatView unmount) — an
  // honest limitation of single-mount ChatView. Surfacing background
  // streaming for chats with no mounted ChatView would require a
  // shell-level SSE on chat lifecycle events, which is out of scope
  // here (the directive: "no new SSE subscriptions or polling").
  const [streamingChatIds, setStreamingChatIds] = useState(() => new Set())

  // Stable callbacks for ChatView — identity must not change across
  // renders or ChatView's onStreamEnd-handler memoization breaks. The
  // setter form lets us avoid depending on the previous state.
  const markStreamingStart = useCallback((chatId) => {
    if (!chatId) return
    setStreamingChatIds(prev => {
      if (prev.has(chatId)) return prev
      const next = new Set(prev)
      next.add(chatId)
      return next
    })
  }, [])
  const markStreamingEnd = useCallback((chatId) => {
    if (!chatId) return
    setStreamingChatIds(prev => {
      if (!prev.has(chatId)) return prev
      const next = new Set(prev)
      next.delete(chatId)
      return next
    })
  }, [])

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
    return queryClient.refetchQueries({ queryKey: appQueries.keys.all })
      .then(() => queryClient.getQueryData(appQueries.keys.all) || [])
      .catch(() => [])
  }, [queryClient])
  const refreshChats = useCallback(() => {
    return queryClient.refetchQueries({ queryKey: chatQueries.keys.all })
      .then(() => queryClient.getQueryData(chatQueries.keys.all) || [])
      .catch(() => [])
  }, [queryClient])

  useEffect(() => {
    if (!chatsQuery.isFetched) return
    setActiveChatId(prev => {
      if (prev && chats.some(c => c.id === prev)) return prev
      return chats[0]?.id || null
    })
    chatsLoadedRef.current = true
  }, [chats, chatsQuery.isFetched, setActiveChatId])

  useEffect(() => { if (drawerOpen) { refreshApps(); refreshChats() } }, [drawerOpen, refreshApps, refreshChats])

  // Capture PWA install prompt if the user hasn't dismissed it.
  useEffect(() => {
    if (localStorage.getItem('pwa-prompt-dismissed')) return
    function onBeforeInstall(e) {
      e.preventDefault()
      setPwaPrompt(e)
    }
    window.addEventListener('beforeinstallprompt', onBeforeInstall)
    return () => window.removeEventListener('beforeinstallprompt', onBeforeInstall)
  }, [])

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
      // Refetch the apps list so the affected app's `updated_at`
      // reflects the server's new state. versionForApp reads from
      // that field, so the iframe URL automatically picks up the
      // new cache-buster on the next render — no separate version
      // counter to keep in sync.
      if (ev.appId) {
        refreshApps().then(updatedApps => {
          const name = updatedApps.find(a => String(a.id) === String(ev.appId))?.name || null
          setBuiltApp({ id: Number(ev.appId), name })
        })
      } else {
        refreshApps()
      }
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
  }, [activeAppId, activeView, drawerOpen, activeChatId, loadTheme, refreshApps])

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
  useEffect(() => {
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

    function onMessage(e) {
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
      try {
        if (/^https?:\/\//.test(target)) path = new URL(target).pathname
      } catch { /* keep target as-is */ }
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
  }, [apps, chats, navTo])

  async function newChat({ draft, forceNew } = {}) {
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
    const empty = !forceNew && [...chats]
      .filter(c => !c.has_messages)
      .sort((a, b) => (b.updated_at || '').localeCompare(a.updated_at || ''))
      [0]
    if (empty) {
      chatId = empty.id
    } else {
      // Spam-click guard: when no empty exists, two rapid taps would
      // race two POSTs and leave an extra empty behind. The in-flight
      // ref short-circuits the second call until the first resolves.
      if (creatingChatRef.current) return
      creatingChatRef.current = true
      try {
        const res = await api.chats.create({ title: 'New chat' })
        const chat = await res.json()
        chatId = chat.id
        await refreshChats()
      } catch {
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
      await newChat()
    }
    await refreshChats()
  }

  // Bootstrap: create an initial chat once the server confirms zero chats exist.
  useEffect(() => {
    if (!chatsLoadedRef.current) return
    if (chats.length === 0 && activeChatId === null) {
      newChat()
    }
  }, [chats])

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
        pwaPrompt={pwaPrompt}
        onPwaInstall={() => {
          // Fire the deferred prompt. userChoice resolves with the
          // outcome but we treat both accept and dismiss as "user
          // engaged" — set the dismiss flag so we don't ask again
          // this session. The browser stops firing
          // beforeinstallprompt after a successful install on its
          // own; the localStorage flag covers the "dismiss"
          // case (no re-fire until next session).
          if (!pwaPrompt) return
          pwaPrompt.prompt()
          pwaPrompt.userChoice.then(() => {
            localStorage.setItem('pwa-prompt-dismissed', '1')
            setPwaPrompt(null)
          })
        }}
        onPwaDismiss={() => {
          localStorage.setItem('pwa-prompt-dismissed', '1')
          setPwaPrompt(null)
        }}
        settingsWarning={providerAuth.anyDisconnected}
      />

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
          <ChatView
            key={activeChatId}
            chatId={activeChatId}
            onStreamEnd={() => {
              // ChatView calls this when the agent turn finishes
              // streaming. Clear the drawer dot for this chat.
              markStreamingEnd(activeChatId)
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
