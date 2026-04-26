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
 * Navigation: drawer is purely visual state, navigation pushes to
 * the browser history, back/forward gestures pop the nav stack.
 *
 * Architecture:
 *   - `openDrawer` / `closeDrawer` only flip a React state flag — no
 *     `pushState`, no `history.back`. The drawer is an overlay, not a
 *     route. This eliminates the +1 history leak the previous
 *     "drawer-as-back-stack" pattern carried, and keeps the visible
 *     UX simple ("back goes to the previous view, not into a drawer
 *     state").
 *   - `navTo(view, opts)` pushes a sentinel history entry AND a
 *     navStack entry capturing where we came from. Each navigation
 *     adds exactly one history entry; each back-gesture pops one.
 *   - `popstate` (or Navigation API `traverse`) treats every back as
 *     "pop the navStack and restore prior view." Drawer is closed as
 *     a side-effect — there is no "back closes the drawer first"
 *     half-step.
 *   - At the bottom of the navStack, the next back-gesture leaves the
 *     PWA naturally (browser default). We don't try to trap it.
 *
 * `backFiredRef` is preserved for the existing Android-back-gesture
 * hack in Shell.jsx (the OS synthesizes a click on the logo ~300ms
 * after a back gesture; the hack ignores that click). Not load-
 * bearing for navigation logic.
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

  function openDrawer() {
    setDrawerOpen(true)
  }

  function closeDrawer() {
    setDrawerOpen(false)
  }

  function navTo(view, opts = {}) {
    navStackRef.current.push({
      view: activeViewRef.current,
      chatId: activeChatIdRef.current,
      appId: activeAppIdRef.current,
    })
    setDrawerOpen(false)
    setActiveView(view)
    if ('chatId' in opts) setActiveChatId(opts.chatId)
    if ('appId' in opts) setActiveAppId(opts.appId)
    // Push exactly one history entry so the next back-gesture has
    // somewhere to land. We pushState AFTER setDrawerOpen(false) so
    // the BFCache snapshot the browser captures of the current entry
    // shows the closed drawer (avoids "two drawers" during the
    // back-gesture animation on Chrome Android).
    try { history.pushState(null, '', '/') } catch { /* ignore */ }
  }

  useEffect(() => {
    // Reset URL to / once on mount — deep-link path was parsed above
    // into state, no need to keep it visible.
    history.replaceState(null, '', '/')

    function applyBack() {
      backFiredRef.current = true
      setTimeout(() => { backFiredRef.current = false }, 400)
      // Drawer is a transient overlay — close it on every nav,
      // regardless of what state we're popping back into.
      drawerOpenRef.current = false
      setDrawerOpen(false)
      const entry = navStackRef.current.pop()
      if (entry) {
        setActiveView(entry.view)
        setActiveChatId(entry.chatId)
        setActiveAppId(entry.appId)
      }
      // navStack empty: nothing to restore. The browser already
      // navigated; if there's no further entry, the PWA exits.
    }

    // Navigation API path (modern Chrome): intercept() suppresses
    // the back-forward slide animation on desktop and gives us a
    // cleaner handler invocation than popstate.
    if (typeof navigation !== 'undefined' && navigation.addEventListener) {
      function onNavigate(e) {
        if (e.navigationType !== 'traverse') return
        if (!e.canIntercept) return
        if (navStackRef.current.length === 0 && !drawerOpenRef.current) return
        e.intercept({ handler() { applyBack() } })
      }
      navigation.addEventListener('navigate', onNavigate)
      return () => navigation.removeEventListener('navigate', onNavigate)
    }

    // popstate fallback (Safari, older Chrome).
    function onPopState() {
      if (navStackRef.current.length === 0 && !drawerOpenRef.current) return
      applyBack()
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
    navStackRef,
    activeViewRef,
    activeChatIdRef,
    activeAppIdRef,
  }
}
