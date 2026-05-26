import { useState, useEffect, useRef, useCallback } from 'react'

const ACTIVE_CHAT_KEY = 'moebius_active_chat'

const MAX_APP_SENTINELS = 20

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
 * Navigation: drawer-as-virtual-route + custom navStack.
 *
 * **READ CLAUDE.md "Navigation — DO NOT CHANGE WITHOUT READING THIS
 * WHOLE SECTION" before editing this file.** It documents the full
 * desiderata, the architecture diagram, the table of rejected
 * alternatives, and the gotchas. This docstring is a summary, not
 * the spec.
 *
 * Three load-bearing pieces:
 *
 *   1. `openDrawer` pushes a sentinel history entry. Browser history
 *      grows by one. Drawer is conceptually a "virtual route" but the
 *      URL stays at `/` (we pass `null, ''` to pushState).
 *
 *   2. `navTo(view, opts)` updates internal state + `navStackRef` and
 *      **does NOT call pushState**. The user stays on the drawer-
 *      sentinel entry while the in-app view changes. This is the
 *      structural fix for the Chrome Android BFCache "two drawers"
 *      swipe-back artifact: Chrome's per-entry snapshot of the base
 *      entry was captured before any drawer opened, so it's clean.
 *
 *   3. Every drawer-close path (X button, overlay tap, OS back-
 *      gesture) funnels through `history.back()` → `handleBack`.
 *      `handleBack` has a **drawer-first guard**: if the drawer was
 *      open with a sentinel, just close drawer and return — do NOT
 *      pop `navStackRef`. This prevents the "tap overlay
 *      unexpectedly navigates to home" regression we shipped earlier.
 *
 * `backFiredRef` is preserved for the Android-back-gesture click
 * synthesis hack in Shell.jsx (the OS sends a click on the logo
 * ~300ms after a back gesture; the hack ignores it).
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
  // True when openDrawer pushed an entry that hasn't been consumed by
  // a navigation or a back-gesture yet.
  const drawerPushedRef = useRef(false)
  // Per-app pending nav-sentinels installed via the moebius:nav-push
  // postMessage protocol. Keyed by appId. AppCanvas validates incoming
  // messages and increments via appNavPush; handleBack consumes via
  // moebius:nav-back postMessage to the iframe. See "Mini-app back-
  // nav protocol" in skill/agent-skill.md.
  const appSentinelCountsRef = useRef(new Map())
  // True while we are programmatically consuming stale app-sentinels
  // via history.go(-N) during an app switch. The popstate handler
  // checks this flag and bails so the auto-pop doesn't fire
  // handleBack and over-pop navStackRef.
  const suppressPopstateRef = useRef(0)

  function openDrawer() {
    history.pushState(null, '')
    drawerPushedRef.current = true
    setDrawerOpen(true)
  }

  function closeDrawer() {
    if (!drawerOpenRef.current) return
    if (drawerPushedRef.current) {
      // Funnel through history.back() so handleBack handles the state
      // transition. This makes back-gesture and overlay-tap follow
      // exactly the same code path through handleBack, with the
      // drawer-first guard there preventing navStack over-pop.
      history.back()
    } else {
      // Defensive: drawer open without a sentinel (shouldn't happen
      // in normal flow). Just close it directly.
      drawerOpenRef.current = false
      setDrawerOpen(false)
    }
  }

  /**
   * Mini-app nav-bridge: install a back-sentinel on behalf of the
   * active mini-app. The iframe's window.postMessage sends
   * moebius:nav-push when entering a nested view (article open,
   * detail modal, etc.). Pushing a real top-level history entry
   * here makes Android's swipe-back gesture snapshot the current
   * view as the preview — which iframe-internal history can't do.
   * On back-gesture, handleBack consumes one of these sentinels by
   * forwarding moebius:nav-back to the iframe instead of changing
   * the shell view.
   */
  // Returns true on success, false on rejection (cap hit). Callers
  // (AppCanvas) post `moebius:nav-push-rejected` back to the iframe
  // on false so the app can correct its own bookkeeping — silent
  // rejection would let the app's count drift permanently above the
  // shell's, and its next `nav-pop` would consume someone else's
  // legit sentinel (which back-fires hard).
  //
  // Wrapped in useCallback with [] deps because Shell passes this
  // function down to AppCanvas, and AppCanvas's message-listener
  // useEffect depends on it. Without the stable identity, the
  // listener tears down + re-registers on every Shell render
  // (every SSE event, every queue update, every toast). The
  // teardown→register window can drop a frame-mounted message →
  // stuck loading spinner. All state we read is via refs, so the
  // empty dep array is correct.
  const appNavPush = useCallback((appId) => {
    if (appId == null) return false
    const m = appSentinelCountsRef.current
    const current = m.get(appId) || 0
    if (current >= MAX_APP_SENTINELS) {
      return false
    }
    try { history.pushState(null, '') } catch { return false }
    m.set(appId, current + 1)
    return true
  }, [])

  /** Consume one app-sentinel (e.g. user tapped the in-app back
   *  button inside the mini-app). Funnels through history.back so
   *  the popstate handler's app-sentinel-first branch sees the same
   *  state as a user gesture. */
  const appNavPop = useCallback((appId) => {
    if (appId == null) return
    const m = appSentinelCountsRef.current
    const n = m.get(appId) || 0
    if (n <= 0) return
    history.back()
  }, [])

  function navTo(view, opts = {}) {
    // Switching apps clears the previous app's pending sentinels —
    // they belong to a view we're leaving. We DO collapse them via
    // history.go(-stale) and suppress the popstate handler for the
    // window in which the synthetic events fire. Without this, the
    // sentinels would sit as orphan history entries: the user would
    // back-gesture into them, handleBack's app-sentinel branch
    // wouldn't trigger (count is 0), the navStack-pop branch would
    // run with an empty navStack, and the UI would feel frozen for
    // `stale` back-presses.
    const newAppId = ('appId' in opts) ? opts.appId : activeAppIdRef.current
    if (activeAppIdRef.current && activeAppIdRef.current !== newAppId) {
      const m = appSentinelCountsRef.current
      const stale = m.get(activeAppIdRef.current) || 0
      if (stale > 0) {
        m.set(activeAppIdRef.current, 0)
        suppressPopstateRef.current += stale
        try { history.go(-stale) } catch { /* ignore */ }
      }
    }
    // Ensure exactly one history entry exists above the current
    // entry to serve as the back-target for this navigation.
    // Two cases:
    //   - drawer was open: openDrawer already pushed a sentinel;
    //     we consume it (clear drawerPushedRef so closeDrawer
    //     doesn't try to history.back() again).
    //   - drawer was closed (e.g. nav from a chat's "Open app"
    //     banner, or from a deep-link entry point with no drawer
    //     interaction yet): push our own back-target sentinel.
    //     Without this, navigating from a drawer-less state leaves
    //     zero entries above base — back-gesture exits the PWA
    //     instead of returning to the prior view. (Real bug seen
    //     in deep-link → onOpenApp flow before this fix.)
    // No BFCache concern in the closed-drawer push path: there's no
    // drawer animation in flight when we pushState, so Chrome's
    // snapshot of the entry-being-left captures the clean view.
    if (drawerPushedRef.current) {
      drawerPushedRef.current = false
    } else {
      try { history.pushState(null, '') } catch { /* ignore */ }
    }
    navStackRef.current.push({
      view: activeViewRef.current,
      chatId: activeChatIdRef.current,
      appId: activeAppIdRef.current,
    })
    drawerOpenRef.current = false
    setDrawerOpen(false)
    // Order matters: set view-payload state (chatId, appId) BEFORE
    // flipping activeView. If React doesn't batch (or batches
    // partially), an early render would see view='canvas' but a
    // stale appId, briefly mounting AppCanvas with the wrong appId
    // and producing a small visible jitter. Setting view last
    // guarantees the conditional rendering only flips when the
    // payload is correct.
    if ('chatId' in opts) setActiveChatId(opts.chatId)
    if ('appId' in opts) setActiveAppId(opts.appId)
    setActiveView(view)
  }

  useEffect(() => {
    // Reset URL to / once on mount — deep-link path was parsed above
    // into state, no need to keep it visible.
    history.replaceState(null, '', '/')

    function handleBack() {
      backFiredRef.current = true
      setTimeout(() => { backFiredRef.current = false }, 400)
      // Drawer-first: if the drawer is open AND its sentinel is the
      // entry being consumed by this back, treat the event as just a
      // drawer-close — don't pop navStack. Catches both real
      // back-gestures on a drawer-open view AND closeDrawer's
      // history.back() (overlay tap, X button). Without this guard,
      // closing the drawer from any deep view over-pops navStack and
      // navigates back unexpectedly.
      if (drawerOpenRef.current && drawerPushedRef.current) {
        drawerPushedRef.current = false
        drawerOpenRef.current = false
        setDrawerOpen(false)
        return
      }
      // App-sentinel-first: if the active mini-app has pending
      // sentinels installed via moebius:nav-push, the current entry
      // being consumed belongs to the app's nested-view state — not
      // the shell's navStack. Forward moebius:nav-back to the iframe
      // and decrement the count. The shell view does NOT change.
      const appId = activeAppIdRef.current
      if (appId != null) {
        const m = appSentinelCountsRef.current
        const n = m.get(appId) || 0
        if (n > 0) {
          m.set(appId, n - 1)
          const iframe = document.querySelector(
            `iframe[data-app-id="${appId}"]`
          )
          if (iframe?.contentWindow) {
            iframe.contentWindow.postMessage(
              { type: 'moebius:nav-back' },
              window.location.origin,
            )
          }
          return
        }
      }
      // No drawer (or drawer open without a sentinel — defensive):
      // treat as real navigation back. Pop navStack and restore.
      drawerPushedRef.current = false
      drawerOpenRef.current = false
      setDrawerOpen(false)
      const entry = navStackRef.current.pop()
      if (entry) {
        // Order matters: set view-payload state (chatId, appId)
        // BEFORE flipping activeView. If React doesn't batch (or
        // batches partially), an early render would see view=
        // 'canvas' but a stale appId, briefly mounting AppCanvas
        // with the wrong appId. Setting view last guarantees the
        // conditional rendering only flips when payload is correct.
        //
        // We can faithfully restore entry.appId (even when null) —
        // the multi-iframe LRU in Shell keeps recently-visited apps
        // mounted regardless of activeAppId, so a transition to
        // null doesn't unmount any iframe; the cached AppCanvas
        // simply becomes hidden until the user re-enters it.
        setActiveChatId(entry.chatId)
        setActiveAppId(entry.appId)
        setActiveView(entry.view)
      }
    }

    // Navigation API path (modern Chrome): intercept() suppresses the
    // back-forward slide on desktop and gives us a cleaner handler
    // invocation than popstate.
    if (typeof navigation !== 'undefined' && navigation.addEventListener) {
      function onNavigate(e) {
        if (e.navigationType !== 'traverse') return
        if (!e.canIntercept) return
        // Synthetic back fired by navTo's stale-sentinel cleanup —
        // decrement and bail. Without this guard the cleanup would
        // pop navStackRef once per stale entry.
        if (suppressPopstateRef.current > 0) {
          suppressPopstateRef.current -= 1
          return
        }
        // Nothing to go back to — let the browser handle it (exits PWA).
        if (navStackRef.current.length === 0
            && !drawerOpenRef.current
            && !(appSentinelCountsRef.current.get(activeAppIdRef.current) > 0)) return
        e.intercept({ handler() { handleBack() } })
      }
      navigation.addEventListener('navigate', onNavigate)
      return () => navigation.removeEventListener('navigate', onNavigate)
    }

    // popstate fallback (Safari, older Chrome).
    function onPopState() {
      if (suppressPopstateRef.current > 0) {
        suppressPopstateRef.current -= 1
        return
      }
      if (navStackRef.current.length === 0
            && !drawerOpenRef.current
            && !(appSentinelCountsRef.current.get(activeAppIdRef.current) > 0)) return
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
    appNavPush,
    appNavPop,
    appSentinelCountsRef,
  }
}
