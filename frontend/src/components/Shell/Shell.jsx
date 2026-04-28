import { useState, useEffect, useCallback, useRef } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import Drawer from '../Drawer/Drawer.jsx'
import AppCanvas from '../AppCanvas/AppCanvas.jsx'
import ChatView from '../ChatView/ChatView.jsx'
import SettingsView from '../SettingsView/SettingsView.jsx'
import { apiFetch, BASE } from '../../api/client.js'
import usePushSubscription from '../../hooks/usePushSubscription.js'
import useNavigation from '../../hooks/useNavigation.js'
import useTheme from '../../hooks/useTheme.js'
import { chatMessagesQueryKey } from '../../hooks/queries.js'
import './Shell.css'

export default function Shell() {
  const {
    activeView, setActiveView,
    activeAppId, setActiveAppId,
    activeChatId, setActiveChatId,
    drawerOpen, openDrawer, closeDrawer,
    navTo, backFiredRef, drawerPushedRef, navStackRef,
    activeViewRef, activeChatIdRef, activeAppIdRef,
  } = useNavigation()

  const { loadTheme } = useTheme()
  const queryClient = useQueryClient()

  const [apps, setApps] = useState([])
  // Per-app version map. Bumped when an `app_updated` SSE event
  // arrives for that specific app so its iframe `key` cycles. The
  // multi-iframe LRU cache (below) needs per-app versions because a
  // single scalar would mis-bust other cached apps' iframes when one
  // gets edited.
  const [appVersions, setAppVersions] = useState({})
  // LRU cache of recently-visited app IDs (most-recent first).
  // Each entry stays mounted as a hidden iframe so re-opening it via
  // drawer-tap or back-nav is instant — no module re-fetch, no
  // WebGL re-init, no app-side data refetch. Bounded by APP_CACHE_MAX
  // to keep memory predictable on phones (each Three.js / WebGL app
  // can hold tens of MB).
  const APP_CACHE_MAX = 4
  const [appCache, setAppCache] = useState([])
  const [toast, setToast] = useState(null)
  const [chats, setChats] = useState([])
  const chatsLoadedRef = useRef(false)
  const [builtApp, setBuiltApp] = useState(null)
  const [pwaPrompt, setPwaPrompt] = useState(null)

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

  const refreshApps = useCallback(() => {
    return apiFetch('/apps/')
      .then((r) => r.json())
      .then((data) => {
        if (Array.isArray(data)) {
          setApps(data)
          return data
        }
        return []
      })
      .catch(() => [])
  }, [])

  // Load chats; restore activeChatId if still present, else pick the first.
  const refreshChats = useCallback(() => {
    apiFetch('/chats')
      .then(r => r.json())
      .then(data => {
        if (!Array.isArray(data)) return
        setChats(data)
        setActiveChatId(prev => {
          if (prev && data.some(c => c.id === prev)) return prev
          return data[0]?.id || null
        })
        chatsLoadedRef.current = true
      })
      .catch(() => {})
  }, [])

  useEffect(() => { refreshApps() }, [refreshApps])
  useEffect(() => { refreshChats() }, [refreshChats])
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
      if (ev.appId) {
        refreshApps().then(updatedApps => {
          const name = updatedApps.find(a => String(a.id) === String(ev.appId))?.name || null
          setBuiltApp({ id: Number(ev.appId), name })
        })
      } else {
        refreshApps()
      }
      // Bump only the affected app's version so its iframe refreshes
      // while other cached apps stay intact. Works whether the app
      // is currently active or just sitting in the LRU cache.
      if (ev.appId) {
        setAppVersions(prev => ({
          ...prev,
          [ev.appId]: (prev[ev.appId] || 0) + 1,
        }))
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
      window.history.replaceState(null, '', '/')
      document.body.style.transition = 'opacity 0.2s ease'
      document.body.style.opacity = '0'
      setTimeout(() => window.location.reload(), 220)
    } else if (ev.type === 'shell_rebuild_failed') {
      setToast('Shell rebuild failed — the agent is fixing it.')
      setTimeout(() => setToast(null), 8000)
    }
  }, [activeAppId, activeView, drawerOpen, activeChatId, loadTheme, refreshApps])

  // Listen for postMessage events from mini-app iframes:
  //   moebius:app-error — route crash report to the chat that built the app
  //     (stored as chat_id on the app record). Falls back to a new chat if
  //     the building chat was deleted. Error is set as a draft (not auto-sent)
  //     so the user can review before sending.
  //   moebius:new-chat — open a new chat with optional pre-filled draft text.
  //     Always forceNew to avoid reusing the current empty chat (which would
  //     skip the useState initializer that reads pending-draft).
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
      if (e.origin !== 'null' && e.origin !== window.location.origin) return
      if (e.data?.type === 'moebius:app-error') {
        handleAppError(e)
      } else if (e.data?.type === 'moebius:new-chat') {
        newChat({ draft: e.data.draft, forceNew: true })
      }
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [apps, chats])

  async function newChat({ draft, forceNew } = {}) {
    // Resolve chatId BEFORE switching views — setting activeView='chat'
    // with the old chatId causes a visible flash of the previous chat.
    // forceNew: always create a new chat (don't reuse empty). Required for
    // moebius:new-chat because reusing the same chatId wouldn't remount
    // ChatView, so the useState initializer wouldn't read pending-draft.
    let chatId
    const empty = !forceNew && [...chats]
      .filter(c => !c.has_messages)
      .sort((a, b) => (b.updated_at || '').localeCompare(a.updated_at || ''))
      [0]

    if (empty) {
      chatId = empty.id
    } else {
      try {
        const res = await apiFetch('/chats', {
          method: 'POST',
          body: JSON.stringify({ title: 'New chat' }),
        })
        const chat = await res.json()
        chatId = chat.id
        refreshChats()
      } catch { return }
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
    await apiFetch(`/chats/${id}`, { method: 'DELETE' }).catch(() => {})
    try { sessionStorage.removeItem(`draft:${id}`) } catch {}
    // Evict the cached messages so a future chat-ID collision (e.g.
    // recovery) can't surface stale content.
    queryClient.removeQueries({ queryKey: chatMessagesQueryKey(id) })
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
    setChats(prev => prev.filter(c => c.id !== id))
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
            onStreamEnd={() => { refreshApps(); loadTheme(); refreshChats() }}
            onFirstMessage={refreshChats}
            onSystemEvent={handleSystemEvent}
            builtApp={builtApp}
            onOpenApp={(appId) => { navTo('canvas', { appId }); setBuiltApp(null) }}
            onMessageStart={() => setBuiltApp(null)}
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
              version={appVersions[id] || 0}
              appName={apps.find(a => String(a.id) === String(id))?.name}
            />
          </div>
        ))}
        {activeView === 'settings' && (
          <SettingsView onThemeChange={loadTheme} />
        )}
      </main>
      {pwaPrompt && (
        <div className="shell__pwa-banner">
          <span>Install Möbius as an app?</span>
          <div className="shell__pwa-actions">
            <button
              className="shell__pwa-btn shell__pwa-btn--install"
              onClick={() => {
                pwaPrompt.prompt()
                pwaPrompt.userChoice.then(() => {
                  localStorage.setItem('pwa-prompt-dismissed', '1')
                  setPwaPrompt(null)
                })
              }}
            >Install</button>
            <button
              className="shell__pwa-btn"
              onClick={() => {
                localStorage.setItem('pwa-prompt-dismissed', '1')
                setPwaPrompt(null)
              }}
            >Not now</button>
          </div>
        </div>
      )}

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
