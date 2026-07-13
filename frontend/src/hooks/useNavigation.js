import { useState, useEffect, useRef, useCallback } from 'react'
import {
  isMobiusNavState,
  pushNavEntry,
  replaceNavEntry,
} from '../lib/navHistory.js'
import { resolveInitialNav } from '../lib/resolveInitialNav.js'

const ACTIVE_CHAT_KEY = 'moebius_active_chat'
const ACTIVE_VIEW_KEY = 'moebius_active_view'
const ACTIVE_APP_KEY = 'moebius_active_app'
const RETURN_VIEW_KEY = 'mobius:return-view'

const MAX_APP_SENTINELS = 20

// Returns true if ANY app in the per-app sentinel map has pending
// back-targets. Used by the popstate / onNavigate early-return guards:
// even when the currently-active app has zero sentinels (e.g. you
// just switched to Notes), an INACTIVE app's sentinels (Klix at
// comment depth) are still real back-targets — the OS back-gesture
// should be intercepted so handleBack can route them properly.
function _anyAppHasSentinels(map) {
  for (const n of map.values()) {
    if (n > 0) return true
  }
  return false
}

// Last active chat id, read defensively (private-mode / disabled storage throws).
function safeStoredChatId() {
  try { return localStorage.getItem(ACTIVE_CHAT_KEY) } catch { return null }
}

// Parse shell-reload state (shell rebuild preserves view across reload).
// Exported so App.jsx can read the parsed value without a second
// sessionStorage.getItem() call — the IIFE already consumed and removed the
// key, so a second read would always return null (dead branch in App.jsx).
export const shellReload = (() => {
  const raw = sessionStorage.getItem('shell-reload')
  if (!raw) return null
  sessionStorage.removeItem('shell-reload')
  try { return JSON.parse(raw) } catch { return null }
})()

// Parse deep-link URL. A COLD notification tap lands on the in-scope
// shell form `/shell/?app=<id>` (or `?chat=<id>`) — this is what reopens
// the installed standalone PWA instead of a browser tab, because it's
// inside the manifest scope (`/shell/`). The legacy out-of-scope forms
// `/app/:id` and `/chat/:id` are still parsed for back-compat (warm taps
// on notifications already in the OS tray, or older senders).
const deepLink = (() => {
  const path = window.location.pathname
  // In-scope cold-start form: /shell/?app=<id> | /shell/?chat=<id>.
  if (/^\/shell\/?$/.test(path)) {
    try {
      const params = new URLSearchParams(window.location.search)
      const app = params.get('app')
      const chat = params.get('chat')
      if (app) return { view: 'canvas', appId: parseInt(app, 10) }
      if (chat) return { view: 'chat', chatId: chat }
    } catch { /* no query — fall through */ }
    return null
  }
  const appMatch = path.match(/^\/app\/([^/]+)$/)
  const chatMatch = path.match(/^\/chat\/([^/]+)$/)
  if (appMatch) return { view: 'canvas', appId: parseInt(appMatch[1], 10) }
  if (chatMatch) return { view: 'chat', chatId: chatMatch[1] }
  return null
})()

// Cold-restore of the active view/app (mirror of moebius_active_chat) so a
// COLD relaunch of the shell PWA lands on the app the user was viewing
// instead of defaulting to a chat. Only the canvas needs an explicit
// signal (chat is the default). shellReload / deepLink (an explicit
// destination for THIS load) take precedence — see below.
const restored = (() => {
  try {
    const view = localStorage.getItem(ACTIVE_VIEW_KEY)
    const app = localStorage.getItem(ACTIVE_APP_KEY)
    if (view === 'canvas' && app) {
      const id = parseInt(app, 10)
      if (Number.isFinite(id)) return { view: 'canvas', appId: id }
    }
  } catch { /* storage unavailable */ }
  return null
})()

const returnView = (() => {
  try {
    const view = sessionStorage.getItem(RETURN_VIEW_KEY)
    sessionStorage.removeItem(RETURN_VIEW_KEY)
    return view === 'settings' ? { view: 'settings' } : null
  } catch {
    return null
  }
})()

// The app id cold-restored to the canvas (null unless the storage-restore
// — not shellReload/deepLink — drove it). The restore is OPTIMISTIC: this
// hook can't see the apps list, so Shell validates this id against the
// live /api/apps list ONCE and demotes a restored-but-uninstalled canvas
// to chat. See ARCHITECTURE.md (Navigation back-stack + drawer model).
export const coldRestoredCanvasAppId =
  (!shellReload?.activeView && !deepLink?.view && restored?.view === 'canvas')
    ? restored.appId
    : null

/**
 * Navigation: drawer-as-virtual-route + custom navStack.
 *
 * **READ ARCHITECTURE.md "Navigation back-stack + drawer model"**
 * before editing this file. It documents the full desiderata, the
 * architecture diagram, the table of rejected alternatives, and the
 * gotchas. This docstring is a summary, not the spec.
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
 *   3. Every drawer-close path (overlay tap, Escape key, swipe-left,
 *      the Shell brand toggle, and the OS back-gesture) funnels
 *      through `history.back()` → `handleBack`.
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
  // Resolve the initial view AND whether HOME must be seeded beneath it as the
  // back-stack root, in ONE place (resolveInitialNav) — enforces "HOME is always
  // the root of the shell back-stack" so a deep entry (notification deep-link,
  // cold-restore, shell-reload) can never strand Back with nothing to pop. Lazy
  // so it's computed exactly once; `seedHome` is consumed by the mount effect.
  const [initialNav] = useState(() => resolveInitialNav({
    shellReload,
    deepLink,
    returnView,
    restored,
    storedChatId: safeStoredChatId(),
  }))
  const [activeView, setActiveView] = useState(initialNav.view)
  const [activeAppId, setActiveAppId] = useState(initialNav.appId)
  const [activeChatId, setActiveChatId] = useState(initialNav.chatId)
  const [drawerOpen, setDrawerOpen] = useState(false)

  // Guards the one-shot HOME seed against a StrictMode double-mount / any
  // remount (pushNavEntry is not idempotent). See the mount effect below.
  const seededHomeRef = useRef(false)

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
  // moebius:nav-back postMessage to the iframe. See the mini-app
  // nav-push/nav-back protocol in
  // backend/scripts/seed-skills/building-apps.md.
  const appSentinelCountsRef = useRef(new Map())
  // A nav-pop initiated by the app still traverses browser history, but that
  // traversal is only acknowledging the app's own close. Keep an explicit FIFO
  // so handleBack can consume it without echoing moebius:nav-back and closing a
  // second nested level. Entries remain until their traversal arrives; clearing
  // them during iframe cleanup would turn an already-scheduled local pop into a
  // shell navigation.
  const appLocalPopsRef = useRef([])
  const appLocalPopInFlightRef = useRef(false)
  const appLocalPopInFlightEntryRef = useRef(null)
  const drawerOpenAfterLocalPopRef = useRef(false)

  function openDrawer() {
    // Do not let a just-issued app history traversal consume a drawer entry
    // pushed after it began. Preserve the user's intent and open once the
    // serialized local-pop pump is idle.
    if (appLocalPopInFlightRef.current) {
      drawerOpenAfterLocalPopRef.current = true
      return
    }
    pushNavEntry('drawer')
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
    try { pushNavEntry('app') } catch { return false }
    m.set(appId, current + 1)
    return true
  }, [])

  const pumpLocalAppPop = useCallback(() => {
    if (appLocalPopInFlightRef.current) return
    if (drawerOpenRef.current) return
    const next = appLocalPopsRef.current.find(
      (entry) => entry.appId === activeAppIdRef.current,
    )
    if (!next) return
    // Select the active app's oldest close rather than the global queue head.
    // Hidden cached apps can legitimately enqueue, but must not block the app
    // whose history entry is current.
    // Descendant frames can leave untagged history entries on either side of
    // an app sentinel. Seek through those first; only a traversal whose source
    // is a tagged app entry consumes the app's sentinel.
    const state = history.state
    if (isMobiusNavState(state) && state.kind !== 'app') return
    next.phase = isMobiusNavState(state) ? 'consume' : 'seek'
    appLocalPopInFlightRef.current = true
    appLocalPopInFlightEntryRef.current = next
    history.back()
  }, [])

  const resumeLocalAppPops = useCallback(() => {
    if (appLocalPopsRef.current.length > 0) {
      pumpLocalAppPop()
      // A queued pop may be waiting for its owning app to become active. That
      // must not starve an unrelated drawer-open intent once no traversal is
      // actually in flight.
      if (appLocalPopInFlightRef.current) return
    }
    if (drawerOpenAfterLocalPopRef.current) {
      drawerOpenAfterLocalPopRef.current = false
      openDrawer()
    }
  }, [pumpLocalAppPop])

  /** Consume one app-sentinel (e.g. user tapped the in-app back
   *  button inside the mini-app). Funnels through history.back so
   *  the popstate handler's app-sentinel-first branch sees the same
   *  state as a user gesture. */
  const appNavPop = useCallback((appId) => {
    if (appId == null) return
    const m = appSentinelCountsRef.current
    const n = m.get(appId) || 0
    if (n <= 0) return
    appLocalPopsRef.current.push({ appId, phase: null })
    pumpLocalAppPop()
  }, [pumpLocalAppPop])

  /** Drop all pending sentinels for an app. AppCanvas calls this in
   *  its useEffect cleanup so an LRU eviction (or any iframe unmount)
   *  clears the ghost count. Without this, the count would outlive
   *  the iframe; later back-gestures while that app is active again
   *  would fire `nav-back` postMessages into a null iframe (silently
   *  consumed, no UI response).
   *
   *  The browser history entries from earlier appNavPush calls are
   *  left in place — once the count is 0, _anyAppHasSentinels in
   *  the popstate guard returns false for that app, so back-gestures
   *  through those orphan entries fall through to the browser's
   *  native handling (no-op, eventually exits the PWA). */
  const appNavReset = useCallback((appId) => {
    if (appId == null) return
    appSentinelCountsRef.current.set(appId, 0)
    // Retire queued closes for an evicted/reset frame so they cannot trap Back
    // or block another app. Keep a traversal that has already started: its
    // eventual popstate still needs to be absorbed rather than reinterpreted
    // as shell navigation.
    const inFlight = appLocalPopInFlightRef.current
      ? appLocalPopInFlightEntryRef.current
      : null
    appLocalPopsRef.current = appLocalPopsRef.current.filter(
      (entry) => entry.appId !== appId || entry === inFlight,
    )
  }, [])

  function navTo(view, opts = {}) {
    // App-sentinels from the previous app stay in browser history.
    // Each one is still a valid back-target for that app — the
    // app's iframe is preserved in the LRU cache, and handleBack
    // routes nav-back to whichever app is active at the moment the
    // sentinel is consumed (which navStack-pop will have restored
    // by the time we get to the app-sentinel branch). This gives
    // browser-style back: backing out of Notes returns to Klix at
    // the depth the user left it.
    //
    // An earlier revision cleared sentinels here via
    // history.go(-stale) + a suppressPopstateRef counter. That
    // caused two bugs: (1) the iframe's nested state went out of
    // sync with the shell's count (back-gesture exited the app
    // instead of unwinding); (2) a real user back-gesture inside
    // the ~10ms synthetic-pop window was silently consumed. Both
    // disappear with the no-clearing model.
    //
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
      try { pushNavEntry('nav') } catch { /* ignore */ }
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
    // Reset URL to /shell/ once on mount — deep-link path was parsed
    // above into state, no need to keep it visible. Must match the
    // manifest scope (`/shell/`), otherwise an installed PWA whose
    // start_url is /shell/ would land at /shell/ → 308 → /shell/ →
    // SPA mount → rewrite back to / → Chromium sees "page outside
    // scope" and may refuse the next manifest update in-place.
    replaceNavEntry('base', '/shell/')

    // Seed HOME as the back-stack root when this load booted into a deep
    // destination (resolveInitialNav.seedHome). Push ONE tagged history entry
    // (so the OS back-gesture is intercepted rather than exiting the PWA) and
    // put a single semantic-home entry under navStack. Now Back from a
    // deep-linked app falls to the chat home once the app has unwound its own
    // sentinels — instead of the old "nothing to go back to → exit PWA" path
    // that, with the canvas restore key, re-landed on the same app (the trap).
    // The home entry carries chatId:null so it is immune to chat-delete
    // scrubbing; handleBack resolves it to the freshest active chat. Guarded by
    // seededHomeRef so a StrictMode double-mount can't push it twice; the back
    // listeners below still register on every effect setup.
    //
    // Accepted minor edge: if a cold-restored canvas app was uninstalled since
    // last session, Shell demotes it to chat (coldRestore check) — the seed then
    // sits under a chat we're already on, so the first Back is a harmless no-op
    // and the second exits. Rare + cosmetic; not worth coupling Shell's demote
    // to the nav seed to shave one Back press.
    if (initialNav.seedHome && !seededHomeRef.current) {
      seededHomeRef.current = true
      try {
        pushNavEntry('nav')
        navStackRef.current = [{ view: 'chat', chatId: null, appId: null, homeSeed: true }]
      } catch { /* history unavailable — leave navStack empty */ }
    }

    function handleBack() {
      backFiredRef.current = true
      setTimeout(() => { backFiredRef.current = false }, 400)
      // Defer the React state flip that toggles the .drawer--open class
      // (and thus starts the CSS close transition) to the next animation
      // frame. handleBack runs inside the Navigation-API intercept
      // handler, where the synchronous traversal commit + the tagged
      // pushNavEntry/updateCurrentEntry mirror write contend with the
      // first frame of the slide. Flipping the class one frame later lets
      // that synchronous work settle first, so the close starts crisp
      // instead of stuttering on the opening frame. The ref is cleared
      // synchronously (it gates nothing visual); only the setState waits.
      const closeDrawerNextFrame = () => {
        if (typeof requestAnimationFrame === 'function') {
          requestAnimationFrame(() => setDrawerOpen(false))
        } else {
          setDrawerOpen(false)
        }
      }
      // Drawer-first: if the drawer is open AND its sentinel is the
      // entry being consumed by this back, treat the event as just a
      // drawer-close — don't pop navStack. Catches both real
      // back-gestures on a drawer-open view AND closeDrawer's
      // history.back() (overlay tap, Escape key, swipe-left, Shell
      // brand toggle). Without this guard,
      // closing the drawer from any deep view over-pops navStack and
      // navigates back unexpectedly.
      if (drawerOpenRef.current && drawerPushedRef.current) {
        drawerPushedRef.current = false
        drawerOpenRef.current = false
        closeDrawerNextFrame()
        // Local app pops are deferred while the drawer is open. Once the one
        // drawer traversal finishes, start exactly one serialized app traversal.
        appLocalPopInFlightRef.current = false
        setTimeout(resumeLocalAppPops, 0)
        return
      }
      // Local-app-pop-first: the app already mutated its own state and asked us
      // to remove the matching shell sentinel. Consume exactly that sentinel,
      // but do not send moebius:nav-back — doing so would close two levels for
      // one in-app close action. Use the queued app id rather than the active id
      // so a render/app switch between request and traversal cannot decrement
      // another app's count.
      const localPop = appLocalPopInFlightRef.current
        ? appLocalPopInFlightEntryRef.current
        : null
      if (localPop) {
        appLocalPopInFlightRef.current = false
        appLocalPopInFlightEntryRef.current = null
        if (localPop.phase === 'seek') {
          // We have reached a tagged entry but have not traversed the app
          // sentinel yet. Re-evaluate the current entry and continue once.
          setTimeout(resumeLocalAppPops, 0)
          return
        }
        appLocalPopsRef.current = appLocalPopsRef.current.filter(
          (entry) => entry !== localPop,
        )
        const m = appSentinelCountsRef.current
        const n = m.get(localPop.appId) || 0
        if (n > 0) m.set(localPop.appId, n - 1)
        setTimeout(resumeLocalAppPops, 0)
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
              '*',
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
        //
        // A homeSeed entry is the SEMANTIC chat home, not a specific chat: it
        // was seeded with chatId:null (so chat-delete scrubbing can't strip it),
        // so resolve it to the freshest active chat at Back-time. A null there
        // (zero-chat instance) is filled by Shell's chat-restore effect.
        setActiveChatId(entry.homeSeed ? activeChatIdRef.current : entry.chatId)
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
        // Phantom-entry guard: ignore a traversal landing on an UNTAGGED
        // entry — one a sandboxed app/preview iframe pushed onto the shared
        // session history. Treating it as our sentinel over-pops navStack.
        if (!isMobiusNavState(e.destination.getState())) {
          // A serialized local pop may have traversed onto a phantom entry.
          // Complete a consume traversal (its tagged app source is gone), or
          // keep seeking until the sentinel itself is current. Do this after
          // the traversal commits so history.state describes the destination.
          if (appLocalPopInFlightRef.current) {
            setTimeout(() => {
              const localPop = appLocalPopInFlightEntryRef.current
              appLocalPopInFlightRef.current = false
              appLocalPopInFlightEntryRef.current = null
              if (localPop?.phase === 'consume') {
                appLocalPopsRef.current = appLocalPopsRef.current.filter(
                  (entry) => entry !== localPop,
                )
                const m = appSentinelCountsRef.current
                const n = m.get(localPop.appId) || 0
                if (n > 0) m.set(localPop.appId, n - 1)
              }
              resumeLocalAppPops()
            }, 0)
          }
          return
        }
        // Nothing to go back to — let the browser handle it (exits PWA).
        // Check every back-target source: navStack (shell-level), open
        // drawer, OR any app's pending sentinels (sentinels for an
        // INACTIVE app are still real back-targets — handleBack will
        // restore the owning app via navStack-pop, then subsequent
        // backs unwind that app's nesting).
        if (navStackRef.current.length === 0
            && !drawerOpenRef.current
            && !_anyAppHasSentinels(appSentinelCountsRef.current)
            && appLocalPopsRef.current.length === 0) return
        e.intercept({ handler() { handleBack() } })
      }
      navigation.addEventListener('navigate', onNavigate)
      return () => navigation.removeEventListener('navigate', onNavigate)
    }

    // popstate fallback (Safari, older Chrome).
    function onPopState() {
      // Phantom-entry guard: a pop landing on an UNTAGGED entry is a
      // phantom pushed onto the shared session history by a sandboxed
      // app/preview iframe, not one of our sentinels — ignore it.
      if (!isMobiusNavState(history.state)) {
        if (appLocalPopInFlightRef.current) {
          setTimeout(() => {
            const localPop = appLocalPopInFlightEntryRef.current
            appLocalPopInFlightRef.current = false
            appLocalPopInFlightEntryRef.current = null
            if (localPop?.phase === 'consume') {
              appLocalPopsRef.current = appLocalPopsRef.current.filter(
                (entry) => entry !== localPop,
              )
              const m = appSentinelCountsRef.current
              const n = m.get(localPop.appId) || 0
              if (n > 0) m.set(localPop.appId, n - 1)
            }
            resumeLocalAppPops()
          }, 0)
        }
        return
      }
      if (navStackRef.current.length === 0
            && !drawerOpenRef.current
            && !_anyAppHasSentinels(appSentinelCountsRef.current)
            && appLocalPopsRef.current.length === 0) return
      handleBack()
    }
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  // initialNav is a stable useState value (no setter); all else is refs.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // A queued close from a hidden cached app becomes safe once shell Back
  // restores that app and its sentinel to the current tagged entry.
  useEffect(() => {
    resumeLocalAppPops()
  }, [activeAppId, resumeLocalAppPops])

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

  // Persist active view + app (mirror of active chat) so a cold relaunch
  // of the shell PWA restores the app the user was on. See ARCHITECTURE.md (Navigation back-stack + drawer model).
  useEffect(() => {
    try { localStorage.setItem(ACTIVE_VIEW_KEY, activeView) } catch { /* ignore */ }
  }, [activeView])
  useEffect(() => {
    try {
      if (activeView === 'canvas' && activeAppId != null) {
        localStorage.setItem(ACTIVE_APP_KEY, String(activeAppId))
      } else if (activeView !== 'canvas') {
        localStorage.removeItem(ACTIVE_APP_KEY)
      }
    } catch { /* ignore */ }
  }, [activeView, activeAppId])

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
    backFiredRef,
    drawerPushedRef,
    drawerOpenRef,
    navStackRef,
    activeViewRef,
    activeChatIdRef,
    activeAppIdRef,
    appNavPush,
    appNavPop,
    appNavReset,
  }
}
