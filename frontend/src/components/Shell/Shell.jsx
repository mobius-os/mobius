import { useState, useEffect, useCallback, useRef } from 'react'
import Drawer from '../Drawer/Drawer.jsx'
import AppCanvas from '../AppCanvas/AppCanvas.jsx'
import ChatView from '../ChatView/ChatView.jsx'
import SettingsView from '../SettingsView/SettingsView.jsx'
import { apiFetch } from '../../api/client.js'
import usePushSubscription from '../../hooks/usePushSubscription.js'
import useNavigation from '../../hooks/useNavigation.js'
import useTheme from '../../hooks/useTheme.js'
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

  const [apps, setApps] = useState([])
  const [pendingReport, setPendingReport] = useState(null)
  const [appVersion, setAppVersion] = useState(0)
  const [toast, setToast] = useState(null)
  const [chats, setChats] = useState([])
  const chatsLoadedRef = useRef(false)
  // Last app built/updated by the agent — shown as an "Open app" CTA in chat.
  const [builtApp, setBuiltApp] = useState(null)
  // PWA install prompt deferred event.
  const [pwaPrompt, setPwaPrompt] = useState(null)

  const activeChat = chats.find(c => c.id === activeChatId) || chats[0]

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

  // Load chats from server.
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

  // PWA install prompt.
  useEffect(() => {
    if (localStorage.getItem('pwa-prompt-dismissed')) return
    function onBeforeInstall(e) {
      e.preventDefault()
      setPwaPrompt(e)
    }
    window.addEventListener('beforeinstallprompt', onBeforeInstall)
    return () => window.removeEventListener('beforeinstallprompt', onBeforeInstall)
  }, [])

  // --- system event handler ---
  // Receives events from the chat SSE stream that are not chat content:
  // theme_updated, app_updated, shell_rebuilt, shell_rebuild_failed.
  // theme_updated: hot-reloads CSS variables (no rebuild, no reload).
  // app_updated: refreshes app list; if the updated app is open in the
  //   canvas, bumps appVersion to trigger iframe cache-bust reload.
  // shell_rebuilt: saves view state to sessionStorage, fades out the
  //   page, then reloads.  On the fresh load, App.jsx and Shell detect
  //   the shell-reload flag and restore state without showing the splash.
  // shell_rebuild_failed: shows a toast — the agent sees the error in
  //   its tool output and will fix the issue.
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
      // Deduplicate: the SSE catch-up burst replays all events including
      // past shell_rebuilt, which would cause an infinite reload loop.
      // Ignore if we already handled one within the last 5 seconds.
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

  // Listen for error reports from mini-app iframes.
  // Routes the error to the chat that last built the app (stored as chatId
  // in the frame), so the agent has full context for the fix.  Falls back
  // to a new/empty chat if the building chat was deleted.
  useEffect(() => {
    async function handleAppError(e) {
      const appEntry = apps.find(a => String(a.id) === String(e.data.appId))
      const appName = appEntry?.name || `app ${e.data.appId}`
      const report = `The app "${appName}" crashed with this error:\n\`\`\`\n${e.data.error}\n\`\`\`\nPlease investigate and fix.`
      setActiveView('chat')

      const buildingChatId = appEntry?.chat_id || e.data.chatId || null
      const buildingChat = buildingChatId && chats.find(c => c.id === buildingChatId)
      if (buildingChat) {
        setActiveChatId(buildingChatId)
        refreshChats()
      } else {
        const empty = [...chats]
          .filter(c => !c.has_messages)
          .sort((a, b) => (b.updated_at || '').localeCompare(a.updated_at || ''))
          [0]
        if (empty) {
          setActiveChatId(empty.id)
          refreshChats()
        } else {
          try {
            const res = await apiFetch('/chats', {
              method: 'POST',
              body: JSON.stringify({ title: 'New chat' }),
            })
            const chat = await res.json()
            setActiveChatId(chat.id)
            refreshChats()
          } catch { /* fall back to current active chat */ }
        }
      }

      setPendingReport(report)
    }

    function onMessage(e) {
      if (e.data?.type !== 'moebius:app-error') return
      // Only accept messages from our sandboxed frames (origin is the string 'null'
      // because they lack allow-same-origin) or from our own origin.
      if (e.origin !== 'null' && e.origin !== window.location.origin) return
      handleAppError(e)
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [apps, chats])

  async function deleteApp(id) {
    await apiFetch(`/apps/${id}`, { method: 'DELETE' })
    if (activeAppId === id) setActiveView('chat')
    refreshApps()
  }

  async function newChat() {
    // pushState already happened in openDrawer if from drawer.
    if (drawerPushedRef.current) {
      drawerPushedRef.current = false
      navStackRef.current.push({
        view: activeViewRef.current,
        chatId: activeChatIdRef.current,
        appId: activeAppIdRef.current,
      })
    }
    setActiveView('chat')
    closeDrawer()

    // Reuse the most recently updated empty chat so a draft typed before
    // navigating away survives (new chat → history → new chat → draft is back).
    const empty = [...chats]
      .filter(c => !c.has_messages)
      .sort((a, b) => (b.updated_at || '').localeCompare(a.updated_at || ''))
      [0]

    let chatId
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
    if (activeChatId === id) {
      await newChat()
    }
    setChats(prev => prev.filter(c => c.id !== id))
  }

  // If no chats exist yet, create one. Guard with chatsLoadedRef so we don't
  // fire before the initial refreshChats resolves (chats=[] is also the
  // pre-fetch state, not just the empty-server state).
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
          <img className="shell__logo" src="/moebius.png" alt="" width="30" height="30" />
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
              pendingReport={pendingReport}
              onReportConsumed={() => setPendingReport(null)}
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
