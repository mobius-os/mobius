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
  const [appVersion, setAppVersion] = useState(0)
  const [toast, setToast] = useState(null)
  const [chats, setChats] = useState([])
  const chatsLoadedRef = useRef(false)
  const [builtApp, setBuiltApp] = useState(null)
  const [pwaPrompt, setPwaPrompt] = useState(null)

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
      loadTheme()
      setAppVersion(v => v + 1)
    } else if (ev.type === 'app_updated') {
      if (ev.appId) {
        refreshApps().then(updatedApps => {
          const name = updatedApps.find(a => String(a.id) === String(ev.appId))?.name || null
          setBuiltApp({ id: Number(ev.appId), name })
        })
      } else {
        refreshApps()
      }
      if (ev.appId && String(ev.appId) === String(activeAppId)) {
        setAppVersion(v => v + 1)
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

    // Push nav stack so back returns to the previous view (skip automatic calls).
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
    // Evict any cached messages for the deleted chat so a future chat
    // ID collision (e.g. recovery) doesn't surface stale content.
    queryClient.removeQueries({ queryKey: chatMessagesQueryKey(id) })
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
        {activeView === 'chat' && activeChatId
          ? <ChatView
              key={activeChatId}
              chatId={activeChatId}
              onStreamEnd={() => { refreshApps(); loadTheme(); refreshChats() }}
              onFirstMessage={refreshChats}
              onSystemEvent={handleSystemEvent}
              builtApp={builtApp}
              onOpenApp={(id) => { navTo('canvas', { appId: id }); setBuiltApp(null) }}
              onMessageStart={() => setBuiltApp(null)}
            />
          : activeView === 'canvas'
            ? <AppCanvas appId={activeAppId} version={appVersion} />
            : activeView === 'settings'
              ? <SettingsView onThemeChange={loadTheme} />
              : null
        }
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
