import { useState, useEffect, useRef } from 'react'

const ACTIVE_CHAT_KEY = 'moebius_active_chat'

// Parse shell-reload state (shell rebuild preserves view across reload).
const shellReload = (() => {
  const raw = sessionStorage.getItem('shell-reload')
  if (!raw) return null
  sessionStorage.removeItem('shell-reload')
  try { return JSON.parse(raw) } catch { return null }
})()

// Parse deep-link URL (push notification taps land on /app/:id or /chat/:id).
const deepLink = (() => {
  const path = window.location.pathname
  const appMatch = path.match(/^\/app\/([^/]+)$/)
  const chatMatch = path.match(/^\/chat\/([^/]+)$/)
  if (appMatch) return { view: 'canvas', appId: parseInt(appMatch[1], 10) }
  if (chatMatch) return { view: 'chat', chatId: chatMatch[1] }
  return null
})()

/**
 * Manages navigation state with a custom back stack.
 *
 * We maintain our own navigation stack instead of using pushState,
 * because Chrome Android caches full page state for each history
 * entry and shows it during the back gesture — including the drawer.
 *
 * A single sentinel entry sits on top of the base entry. When back
 * fires, popstate runs, we apply state from our stack, and re-push
 * the sentinel. Chrome only ever sees one cached page (the sentinel).
 */
export default function useNavigation() {
  const [activeView, setActiveView] = useState(
    shellReload?.activeView || deepLink?.view || 'chat'
  )
  const [activeAppId, setActiveAppId] = useState(
    shellReload?.activeAppId || deepLink?.appId || null
  )
  const [activeChatId, setActiveChatId] = useState(
    () => shellReload?.activeChatId || deepLink?.chatId || localStorage.getItem(ACTIVE_CHAT_KEY) || null
  )
  const [drawerOpen, setDrawerOpen] = useState(false)

  const navStackRef = useRef([])
  const activeChatIdRef = useRef(activeChatId)
  activeChatIdRef.current = activeChatId
  const activeViewRef = useRef(activeView)
  activeViewRef.current = activeView
  const activeAppIdRef = useRef(activeAppId)
  activeAppIdRef.current = activeAppId
  const drawerOpenRef = useRef(drawerOpen)
  drawerOpenRef.current = drawerOpen
  // Android back gesture synthesizes a click on the logo ~300ms later.
  const backFiredRef = useRef(false)
  // True when openDrawer pushed an entry that hasn't been consumed by navTo.
  const drawerPushedRef = useRef(false)

  // pushState when the drawer opens — page is clean (no drawer, correct
  // chat visible), so Chrome Android caches the right back-forward preview.
  // If the user closes without navigating, the entry becomes a harmless
  // no-op on back (stack is empty, nothing changes visually).
  function openDrawer() {
    history.pushState(null, '')
    drawerPushedRef.current = true
    setDrawerOpen(true)
  }

  function closeDrawer() {
    const wasPushed = drawerPushedRef.current
    drawerPushedRef.current = false
    // Update the ref synchronously so the popstate handler fired by
    // history.back() below sees drawer-closed state and early-returns
    // without running handleBack.
    drawerOpenRef.current = false
    setDrawerOpen(false)
    // Pop the sentinel entry that openDrawer pushed. Without this, every
    // open/close cycle leaks one history entry — the user has to press
    // back once per toggle before the app actually navigates back.
    if (wasPushed) history.back()
  }

  function navTo(view, opts = {}) {
    drawerPushedRef.current = false
    navStackRef.current.push({
      view: activeViewRef.current,
      chatId: activeChatIdRef.current,
      appId: activeAppIdRef.current,
    })
    setDrawerOpen(false)
    setActiveView(view)
    if ('chatId' in opts) setActiveChatId(opts.chatId)
    if ('appId' in opts) setActiveAppId(opts.appId)
  }

  useEffect(() => {
    history.replaceState(null, '', '/')

    function handleBack() {
      backFiredRef.current = true
      setTimeout(() => { backFiredRef.current = false }, 400)
      // Clear the drawer-pushed flag — the history entry openDrawer
      // pushed is now being consumed by this back event. Without this,
      // a stale true leaks into the next closeDrawer and triggers a
      // spurious history.back().
      drawerPushedRef.current = false
      drawerOpenRef.current = false
      setDrawerOpen(false)
      const entry = navStackRef.current.pop()
      if (entry) {
        setActiveView(entry.view)
        setActiveChatId(entry.chatId)
        setActiveAppId(entry.appId)
      }
    }

    // Navigation API intercept() suppresses Chrome's back-forward slide
    // on desktop. When available, use exclusively (no popstate) to avoid
    // double-popping the nav stack.
    if (typeof navigation !== 'undefined' && navigation.addEventListener) {
      function onNavigate(e) {
        if (e.navigationType !== 'traverse') return
        if (!e.canIntercept) return
        // Nothing to go back to — let the browser handle it (exits PWA).
        if (navStackRef.current.length === 0 && !drawerOpenRef.current) return
        e.intercept({ handler() { handleBack() } })
      }
      navigation.addEventListener('navigate', onNavigate)
      return () => navigation.removeEventListener('navigate', onNavigate)
    }

    // Fallback for browsers without Navigation API.
    function onPopState() {
      if (navStackRef.current.length === 0 && !drawerOpenRef.current) return
      handleBack()
    }
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [])

  // Fade back in after shell-reload.
  useEffect(() => {
    if (!shellReload) return
    document.body.style.transition = 'opacity 0.2s ease'
    document.body.style.opacity = '1'
  }, [])

  // Persist active chat id locally.
  useEffect(() => {
    if (activeChatId) localStorage.setItem(ACTIVE_CHAT_KEY, activeChatId)
  }, [activeChatId])

  return {
    activeView,
    setActiveView,
    activeAppId,
    setActiveAppId,
    activeChatId,
    setActiveChatId,
    drawerOpen,
    openDrawer,
    closeDrawer,
    navTo,
    canGoBack: navStackRef.current.length > 0,
    backFiredRef,
    drawerPushedRef,
    navStackRef,
    activeViewRef,
    activeChatIdRef,
    activeAppIdRef,
  }
}
