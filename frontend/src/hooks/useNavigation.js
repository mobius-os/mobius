import { useState, useEffect, useRef, useCallback } from 'react'
import {
  isMobiusNavState,
  isTopmostAppEntry,
  navEntryId,
  navTraversalDirection,
  ownerKeyOf,
  pushNavEntry,
  replaceNavEntry,
  updateCurrentNavEntry,
} from '../lib/navHistory.js'
import { resolveInitialNav } from '../lib/resolveInitialNav.js'
import * as tabModel from '../components/Shell/tabModel.js'
import * as paneModel from '../components/Shell/paneModel.js'

const ACTIVE_CHAT_KEY = 'moebius_active_chat'
const ACTIVE_VIEW_KEY = 'moebius_active_view'
const ACTIVE_APP_KEY = 'moebius_active_app'
const RETURN_VIEW_KEY = 'mobius:return-view'

const MAX_APP_SENTINELS = 20

// Returns true if ANY (paneId, appId) owner in the per-owner sentinel map has
// pending back-targets. Used by the popstate / onNavigate early-return guards:
// even when the currently-visible app has zero sentinels (e.g. you just switched
// to Notes), an INACTIVE app's sentinels (Klix at comment depth) are still real
// back-targets — the OS back-gesture should be intercepted so handleBack can
// route them properly.
function _anyAppHasSentinels(map) {
  for (const n of map.values()) {
    if (n > 0) return true
  }
  return false
}

// A restorable route carries a `paneId` HINT (design §5). The hint is never a
// foreign key — it may be stale after a close, move, reload, or Forward — so no
// restore fails solely because the hinted pane is dead; `restoreRoute` degrades
// a dead hint to the focused pane.
function navRoute(view, chatId, appId, paneId, extra = null) {
  return {
    view,
    chatId: chatId ?? null,
    appId: appId ?? null,
    paneId: typeof paneId === 'string' ? paneId : null,
    ...(extra || {}),
  }
}

function isRestorableRoute(route) {
  return route && ['chat', 'canvas', 'settings'].includes(route.view)
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
  try {
    const raw = sessionStorage.getItem('shell-reload')
    if (!raw) return null
    sessionStorage.removeItem('shell-reload')
    try { return JSON.parse(raw) } catch { return null }
  } catch {
    return null
  }
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
 * Navigation: an ADAPTER above the workspace reducer (design §1 ownership
 * boundary) + drawer-as-virtual-route + custom navStack.
 *
 * **READ ARCHITECTURE.md "Navigation back-stack + drawer model"** and
 * docs/design/split-pane-workspace.md §1/§5 before editing this file.
 *
 * The workspace reducer (paneModel.js) is the single live authority for what is
 * on screen: pane contents, per-pane active tabs, and `focusedPaneId`. This hook
 * OWNS only navigation mechanics — drawer state, the Settings overlay flag,
 * history refs/queues, and the per-(pane,app) sentinel registry — and DERIVES
 * the legacy `{activeView, activeChatId, activeAppId}` triple from the focused
 * pane's active tab. `navTo`/`restoreRoute` dispatch workspace actions rather
 * than owning parallel state; Settings is the only view state navigation owns.
 *
 * Args:
 *   workspace           the render-time `ws` used to derive the returned triple.
 *   workspaceStateRef   the synchronously-advanced reducer state (`{ws, undo}`)
 *                       that event/history callbacks read as `.current.ws`.
 *   dispatchWorkspace   Shell's synchronous dispatch adapter (advances the ref
 *                       before the raw reducer dispatch), never raw React
 *                       dispatch — so two events in one React batch observe each
 *                       other.
 *   visiblePaneIds      the renderer's committed set of visible pane ids.
 *
 * Three load-bearing pieces remain: `openDrawer` pushes a sentinel history
 * entry; `navTo` updates internal state + `navStackRef` and does NOT call
 * pushState (the BFCache "two drawers" fix); every drawer-close path funnels
 * through `history.back()` → `handleBack`'s drawer-first guard.
 */
export default function useNavigation({
  workspace,
  workspaceStateRef,
  dispatchWorkspace,
  visiblePaneIds,
}) {
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
  // Settings is the ONLY view state navigation owns globally (§1). It is a full-
  // workspace overlay: `activeChatId`/`activeAppId` still describe the focused
  // pane's active tab behind it, but every visibility consumer gates on
  // `activeView`.
  const [settingsOpen, setSettingsOpen] = useState(initialNav.view === 'settings')
  const [drawerOpen, setDrawerOpen] = useState(false)

  // ── Derived legacy triple (the projection, design §1) ────────────────────
  const contentRoute = paneModel.focusedContentRoute(workspace)
  const activeView = settingsOpen ? 'settings' : contentRoute.view
  const activeChatId = contentRoute.chatId
  const activeAppId = contentRoute.appId

  // Guards the one-shot HOME seed against a StrictMode double-mount / any
  // remount (pushNavEntry is not idempotent). See the mount effect below.
  const seededHomeRef = useRef(false)
  const historyInitializedRef = useRef(false)

  const navStackRef = useRef([])
  // The refs mirror the render-time projection so Shell's asynchronous callbacks
  // (system events, shell-reload snapshot, restore probes) read the current
  // focused-pane triple without a stale closure. Assigned during render:
  // idempotent, no side effect.
  const activeChatIdRef = useRef(activeChatId)
  activeChatIdRef.current = activeChatId
  const activeViewRef = useRef(activeView)
  activeViewRef.current = activeView
  const activeAppIdRef = useRef(activeAppId)
  activeAppIdRef.current = activeAppId
  const settingsOpenRef = useRef(settingsOpen)
  settingsOpenRef.current = settingsOpen
  const drawerOpenRef = useRef(drawerOpen)
  drawerOpenRef.current = drawerOpen
  // The committed set of visible pane ids (excludes phone-deck-hidden panes).
  // `isVisibleApp` reads this; Settings-open covered panes are excluded by the
  // separate settingsOpenRef check, not by this set.
  const visiblePaneIdsRef = useRef(visiblePaneIds)
  visiblePaneIdsRef.current = visiblePaneIds
  // Non-authoritative: the last non-null active chat id, used ONLY to resolve
  // the semantic-home (`homeSeed`) route. It never decides what renders while a
  // pane has an active tab.
  const lastChatIdRef = useRef(initialNav.chatId ?? safeStoredChatId())
  if (activeChatId) lastChatIdRef.current = activeChatId

  // Android back gesture synthesizes a click on the logo ~300ms later.
  const backFiredRef = useRef(false)
  // True when openDrawer pushed an entry that hasn't been consumed by
  // a navigation or a back-gesture yet.
  const drawerPushedRef = useRef(false)
  // Per-(pane, app) pending nav-sentinel counts installed via the
  // moebius:nav-push postMessage protocol. Keyed by ownerKeyOf(paneId, appId).
  const appSentinelCountsRef = useRef(new Map())
  // entryId -> { paneId, appId, status:'live'|'consumed'|'retired' }. The
  // History API cannot enumerate old entries during eviction, so this registry
  // records every physical app entry's owner and lifecycle. Every count
  // increment/decrement/retirement updates both structures exactly once.
  const appEntryOwnersRef = useRef(new Map())
  // A nav-pop initiated by the app still traverses browser history, but that
  // traversal is only acknowledging the app's own close. One GLOBAL FIFO (there
  // is one browser session-history cursor) so handleBack can consume it without
  // echoing moebius:nav-back and closing a second nested level.
  const appLocalPopsRef = useRef([])
  const appLocalPopInFlightRef = useRef(false)
  const appLocalPopInFlightEntryRef = useRef(null)
  const localPopSeqRef = useRef(0)
  const drawerOpenAfterLocalPopRef = useRef(false)
  // Last tagged entry reached by the shell. popstate does not expose its source
  // entry, so this is the fallback browser's direction cursor. It deliberately
  // stays put while traversing iframe-created phantom entries; the next tagged
  // destination can then still be compared with the last shell position.
  const currentNavStateRef = useRef(null)
  // An app-local level is destructive: Back tells the iframe to close it, and
  // Forward cannot recreate it. Remember the unique physical entry so a later
  // Forward -> Back traversal is absorbed instead of over-popping shell state.
  // Retired (evicted-frame) entries are kept here for the page lifetime too.
  const consumedAppEntryIdsRef = useRef(new Set())

  // Visibility gate: an app is visible iff Settings is closed, the app is its
  // pane's active tab, and that pane is in the renderer's committed visible set
  // (design §5, contract §3.1.1). NOT "mounted in the cache" and NOT merely
  // "contained by a pane".
  const isVisibleApp = useCallback((ws, appId) => {
    if (settingsOpenRef.current) return false
    if (appId == null) return false
    const key = tabModel.tabKey(tabModel.makeTab('app', appId))
    const pane = paneModel.paneOf(ws, key)
    return !!pane
      && pane.activeTabKey === key
      && visiblePaneIdsRef.current.has(pane.id)
  }, [])

  // Register a new live app entry: record its owner + increment its count. Both
  // structures, exactly once (contract §3.1.3).
  const addAppEntry = useCallback((entryId, paneId, appId) => {
    if (!entryId) return
    appEntryOwnersRef.current.set(entryId, {
      paneId: String(paneId), appId: String(appId), status: 'live',
    })
    const key = ownerKeyOf(paneId, appId)
    const m = appSentinelCountsRef.current
    m.set(key, (m.get(key) || 0) + 1)
  }, [])

  // Consume one physical app entry: decrement its owner count, flip its registry
  // status, and remember its id so a Forward->Back re-absorbs it. `ownerKey` is
  // passed when the caller already resolved it (the entry may have moved panes,
  // so its ORIGINAL owner key governs accounting even when focus is elsewhere).
  const consumeAppEntry = useCallback((entryId, ownerKey) => {
    if (!entryId) return
    const rec = appEntryOwnersRef.current.get(entryId)
    const key = ownerKey || (rec ? ownerKeyOf(rec.paneId, rec.appId) : null)
    if (key) {
      const m = appSentinelCountsRef.current
      const n = m.get(key) || 0
      if (n === 1) m.delete(key)
      else if (n > 1) m.set(key, n - 1)
    }
    if (rec && rec.status === 'live') rec.status = 'consumed'
    consumedAppEntryIdsRef.current.add(entryId)
  }, [])

  // The newest still-live physical entry for an app, scanned in reverse
  // insertion order. Its ORIGINAL owner (not the app's current pane) governs a
  // nav-pop, because a no-reparent move can leave older entries tagged to the
  // prior pane (contract §3.3.1).
  const newestLiveEntryForApp = useCallback((appId) => {
    const target = String(appId)
    const entries = [...appEntryOwnersRef.current.entries()]
    for (let i = entries.length - 1; i >= 0; i -= 1) {
      const [entryId, rec] = entries[i]
      if (rec.appId === target && rec.status === 'live') {
        return { entryId, paneId: rec.paneId, appId: rec.appId }
      }
    }
    return null
  }, [])

  // Retire every live physical entry for an app before its iframe unmounts
  // (contract §4). Marks them consumed (so handleBack discards them atomically),
  // drops all of the app's owner-count keys, and clears its not-yet-started
  // queued pops. An already-in-flight traversal is kept only enough to absorb
  // its inevitable popstate — its target is remembered-consumed and the frame is
  // never messaged. Idempotent: re-running for already-retired records is a
  // no-op, so AppCanvas's unmount cleanup can call it as a backstop.
  const retireAppHistory = useCallback((appId) => {
    const target = String(appId)
    for (const [entryId, rec] of appEntryOwnersRef.current) {
      if (rec.appId === target && rec.status === 'live') {
        consumedAppEntryIdsRef.current.add(entryId)
        rec.status = 'retired'
      }
    }
    const m = appSentinelCountsRef.current
    for (const key of [...m.keys()]) {
      try {
        const parsed = JSON.parse(key)
        if (Array.isArray(parsed) && String(parsed[1]) === target) m.delete(key)
      } catch { /* ignore a malformed key */ }
    }
    const inFlight = appLocalPopInFlightRef.current ? appLocalPopInFlightEntryRef.current : null
    appLocalPopsRef.current = appLocalPopsRef.current.filter(
      (req) => req.appId !== target || req === inFlight,
    )
    if (inFlight && inFlight.appId === target) {
      consumedAppEntryIdsRef.current.add(inFlight.targetEntryId)
    }
  }, [])

  const snapshotRoute = useCallback(() => {
    const content = paneModel.focusedContentRoute(workspaceStateRef.current.ws)
    // When Settings is open the physical route is a Settings route, but it
    // retains the focused content ids + pane hint (contract §2.2.1).
    const view = settingsOpenRef.current ? 'settings' : content.view
    return navRoute(view, content.chatId, content.appId, content.paneId)
  }, [workspaceStateRef])

  const pushShellEntry = useCallback((kind, route) => {
    const state = pushNavEntry(kind, route, {
      currentState: currentNavStateRef.current,
    })
    currentNavStateRef.current = state
    return state
  }, [])

  function openDrawer() {
    // Do not let a just-issued app history traversal consume a drawer entry
    // pushed after it began. Preserve the user's intent and open once the
    // serialized local-pop pump is idle.
    if (appLocalPopInFlightRef.current) {
      drawerOpenAfterLocalPopRef.current = true
      return
    }
    pushShellEntry('drawer', snapshotRoute())
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
   * Mini-app nav-bridge: install a back-sentinel on behalf of a VISIBLE
   * mini-app. Pushing a real top-level history entry makes Android's swipe-back
   * gesture snapshot the current view as the preview — which iframe-internal
   * history can't do. On back-gesture, handleBack consumes one of these
   * sentinels by forwarding moebius:nav-back to the iframe.
   *
   * Returns true on success, false on rejection (not visible, or cap hit).
   * `appNavPush(appId)` keeps the AppCanvas callback signature; the owner pane is
   * DERIVED from the workspace, so a stale `paneId` prop during a no-reparent
   * cross-pane move can't mis-route the install (contract §3.2.1).
   *
   * Kept referentially stable (all state read via refs) because AppCanvas's
   * message-listener effect depends on it — a churning identity would tear it
   * down and re-register on every Shell render, dropping frame-mounted messages.
   */
  const appNavPush = useCallback((appId) => {
    if (appId == null) return false
    const ws = workspaceStateRef.current.ws
    if (!isVisibleApp(ws, appId)) return false
    const key = tabModel.tabKey(tabModel.makeTab('app', appId))
    const pane = paneModel.paneOf(ws, key)
    if (!pane) return false
    const ownerKey = ownerKeyOf(pane.id, appId)
    if ((appSentinelCountsRef.current.get(ownerKey) || 0) >= MAX_APP_SENTINELS) return false
    // Focus the visible owner pane (design §5: an app gesture focuses its pane).
    dispatchWorkspace({ type: 'FOCUS', paneId: pane.id })
    let state
    try {
      state = pushShellEntry('app', navRoute('canvas', null, Number(appId), pane.id))
    } catch { return false }
    addAppEntry(navEntryId(state), pane.id, appId)
    return true
  }, [addAppEntry, dispatchWorkspace, isVisibleApp, pushShellEntry, workspaceStateRef])

  const pumpLocalAppPop = useCallback(() => {
    if (appLocalPopInFlightRef.current) return
    if (drawerOpenRef.current) return
    const next = appLocalPopsRef.current[0]   // ONLY the global FIFO head
    if (!next) return
    const ws = workspaceStateRef.current.ws
    if (!isVisibleApp(ws, next.appId)) return
    const state = history.state
    if (isMobiusNavState(state)) {
      // A tagged entry for another kind, or another owner's app entry, is a hard
      // stop — queue until this request's own tagged entry is topmost.
      if (state.kind !== 'app') return
      if (!isTopmostAppEntry({
        state,
        head: next,
        inFlight: appLocalPopInFlightRef.current,
        drawerOpen: drawerOpenRef.current,
        registry: appEntryOwnersRef.current,
        consumed: consumedAppEntryIdsRef.current,
      })) return
      next.phase = 'consume'
    } else {
      // Untagged phantom on top: seek toward the nearest tagged source.
      next.phase = 'seek'
    }
    appLocalPopInFlightRef.current = true
    appLocalPopInFlightEntryRef.current = next
    history.back()
  }, [isVisibleApp, workspaceStateRef])

  const resumeLocalAppPops = useCallback(() => {
    if (appLocalPopsRef.current.length > 0) {
      pumpLocalAppPop()
      // A queued pop may be waiting for its owning app to become visible. That
      // must not starve an unrelated drawer-open intent once no traversal is
      // actually in flight.
      if (appLocalPopInFlightRef.current) return
    }
    if (drawerOpenAfterLocalPopRef.current) {
      drawerOpenAfterLocalPopRef.current = false
      openDrawer()
    }
  }, [pumpLocalAppPop])

  /** Consume one app-sentinel (e.g. user tapped the in-app back button inside
   *  the mini-app). Enqueues one FIFO request for the newest live physical entry
   *  of that app and calls the pump; no-op if the app has no live entry. */
  const appNavPop = useCallback((appId) => {
    if (appId == null) return
    const target = newestLiveEntryForApp(String(appId))
    if (!target) return
    appLocalPopsRef.current.push({
      requestId: (localPopSeqRef.current += 1),
      targetEntryId: target.entryId,
      appId: String(target.appId),
      ownerKey: ownerKeyOf(target.paneId, target.appId),
      paneId: String(target.paneId),
      phase: 'queued',
    })
    pumpLocalAppPop()
  }, [newestLiveEntryForApp, pumpLocalAppPop])

  /** AppCanvas calls this on iframe unmount / live-frame swap. It retires the
   *  frame's physical history so orphan entries never route Back into a dead
   *  browsing context (contract §4.1.1). */
  const appNavReset = useCallback((appId) => {
    if (appId == null) return
    retireAppHistory(appId, 'reset')
  }, [retireAppHistory])

  function navTo(view, opts = {}) {
    // App-sentinels from other apps stay in browser history — each is still a
    // valid back-target for its owner, routed by its tag when consumed. An
    // earlier revision cleared sentinels here via history.go(-stale) + a
    // suppress-popstate counter; that desynchronized iframe depth and swallowed
    // a real user Back in the synthetic-pop window (regression history at §5.3.1).
    const ws = workspaceStateRef.current.ws
    const targetPaneId = (typeof opts.paneId === 'string' && ws.panes[opts.paneId])
      ? opts.paneId
      : ws.focusedPaneId
    const previousRoute = snapshotRoute()

    let nextRoute
    let openTab = null

    if (view === 'settings') {
      // The destination is a Settings route; its paneId is the focused pane hint
      // behind the overlay.
      nextRoute = navRoute('settings', previousRoute.chatId, previousRoute.appId, targetPaneId)
    } else if (view === 'canvas') {
      const appId = 'appId' in opts ? opts.appId : activeAppIdRef.current
      if (appId == null) return  // reject a malformed payload before any write
      const tab = tabModel.makeTab('app', appId)
      const { opts: target } = tabModel.tabNavTarget(tab)
      nextRoute = navRoute('canvas', null, target.appId, targetPaneId)
      openTab = tab
    } else if (view === 'chat') {
      const chatId = 'chatId' in opts ? opts.chatId : activeChatIdRef.current
      if (chatId == null) return  // reject a malformed payload before any write
      nextRoute = navRoute('chat', String(chatId), null, targetPaneId)
      openTab = tabModel.makeTab('chat', chatId)
    } else {
      return
    }

    // Ensure exactly one history entry sits above the current one to serve as
    // this navigation's back-target: retag a consumed drawer sentinel, else push
    // a fresh nav entry. Exactly one pushState/retag per navTo (§5.3.12).
    if (drawerPushedRef.current) {
      drawerPushedRef.current = false
      currentNavStateRef.current = updateCurrentNavEntry(nextRoute, { kind: 'nav' })
    } else {
      try {
        pushShellEntry('nav', nextRoute)
      } catch { /* history unavailable — leave the entry as-is */ }
    }
    navStackRef.current.push(previousRoute)
    drawerOpenRef.current = false
    setDrawerOpen(false)

    // One reducer action makes payload+view atomic (§1.3.2). Focusing/switching a
    // pane must not close Settings, but a chat/canvas nav always does.
    if (view === 'settings') {
      setSettingsOpen(true)
    } else {
      setSettingsOpen(false)
      dispatchWorkspace({ type: 'OPEN_TAB', paneId: targetPaneId, tab: openTab, activate: true })
    }
  }

  // Restore a route on Back/Forward. Hoisted (not defined in the mount effect)
  // and stable so the history listeners capture one identity. Settings-only
  // routes flip the overlay flag; chat/canvas routes dispatch exactly one
  // pane-targeted OPEN_TAB (workspace-wide dedup follows a moved tab to its
  // current owner) and close Settings (contract §2.3).
  const restoreRoute = useCallback((route) => {
    if (!isRestorableRoute(route)) return
    if (route.view === 'settings') {
      setSettingsOpen(true)
      return
    }
    const ws = workspaceStateRef.current.ws
    const paneId = ws.panes[route.paneId] ? route.paneId : ws.focusedPaneId
    let tab = null
    if (route.view === 'canvas') {
      if (route.appId != null) tab = tabModel.makeTab('app', route.appId)
    } else {
      // A homeSeed entry is the SEMANTIC chat home, not a specific chat: resolve
      // it to the freshest active chat at Back-time. A still-null semantic home
      // leaves the empty chat surface rather than fabricating an id.
      const chatId = route.homeSeed ? lastChatIdRef.current : route.chatId
      if (chatId != null) tab = tabModel.makeTab('chat', chatId)
    }
    if (tab) {
      dispatchWorkspace({ type: 'OPEN_TAB', paneId, tab, activate: true })
    } else {
      // No target id (empty semantic home, or a stray canvas route with no app):
      // focus the hinted/current pane and show whatever it holds.
      dispatchWorkspace({ type: 'FOCUS', paneId })
    }
    setSettingsOpen(false)
  }, [dispatchWorkspace, workspaceStateRef])

  useEffect(() => {
    const bootPaneId = workspaceStateRef.current.ws.focusedPaneId
    if (!historyInitializedRef.current) {
      historyInitializedRef.current = true
      // An EXPLICIT deep link (notification tap, PWA launch-at-app) opens its
      // target into the focused pane, overriding the persisted workspace focus
      // (§5.3.11). A shell-reload snapshot is NOT an override — the workspace
      // blob already restored those tabs. The resolved initial target (last-
      // viewed canvas OR stored chat) is honored ONLY as a fallback: when the
      // focused pane is empty (workspace blob absent/invalid, §5.3.10); a live
      // workspace's focus always wins. A seeded chat is then validated against
      // the live list by Shell's chat-restore effect.
      const bootPaneEmpty = !workspaceStateRef.current.ws.panes[bootPaneId]?.activeTabKey
      if (deepLink?.view === 'canvas' && deepLink.appId != null) {
        dispatchWorkspace({
          type: 'OPEN_TAB', paneId: bootPaneId,
          tab: tabModel.makeTab('app', deepLink.appId), activate: true,
        })
      } else if (deepLink?.view === 'chat' && deepLink.chatId) {
        dispatchWorkspace({
          type: 'OPEN_TAB', paneId: bootPaneId,
          tab: tabModel.makeTab('chat', deepLink.chatId), activate: true,
        })
      } else if (bootPaneEmpty && initialNav.view === 'canvas' && initialNav.appId != null) {
        dispatchWorkspace({
          type: 'OPEN_TAB', paneId: bootPaneId,
          tab: tabModel.makeTab('app', initialNav.appId), activate: true,
        })
      } else if (bootPaneEmpty && initialNav.chatId != null) {
        dispatchWorkspace({
          type: 'OPEN_TAB', paneId: bootPaneId,
          tab: tabModel.makeTab('chat', initialNav.chatId), activate: true,
        })
      }

      // Reset URL to /shell/ once on mount (must match the manifest scope). The
      // deep-link path is now in workspace/Settings state, no need to keep it
      // visible.
      const initialRoute = snapshotRoute()
      const baseRoute = initialNav.seedHome
        ? navRoute('chat', lastChatIdRef.current, null, bootPaneId)
        : initialRoute
      currentNavStateRef.current = replaceNavEntry('base', '/shell/', baseRoute)

      // Seed HOME as the back-stack root when this load booted into a deep
      // destination (canvas/settings) so Back always reaches the chat surface.
      // The home entry carries chatId:null so it is immune to chat-delete
      // scrubbing; handleBack resolves it to the freshest active chat.
      if (initialNav.seedHome && !seededHomeRef.current) {
        seededHomeRef.current = true
        try {
          pushShellEntry('nav', initialRoute)
          navStackRef.current = [navRoute('chat', null, null, null, { homeSeed: true })]
        } catch { /* history unavailable — leave navStack empty */ }
      }
    } else {
      // StrictMode re-runs effect setup. Do not replace the already-pushed deep
      // destination with another base entry.
      currentNavStateRef.current = history.state
    }

    function handleForward(destination, sourceRoute) {
      const route = destination?.route
      // A nav entry represents a shell-level transition. Back destructively
      // removed its source from navStack, so Forward rebuilds that one edge.
      // App entries do not: they represent nested iframe state that the host
      // cannot recreate once the app consumed moebius:nav-back.
      if (destination?.kind === 'nav' && isRestorableRoute(sourceRoute)) {
        navStackRef.current.push(sourceRoute)
      }
      restoreRoute(route)
      if (destination?.kind === 'drawer') {
        drawerPushedRef.current = true
        drawerOpenRef.current = true
        setDrawerOpen(true)
      } else {
        drawerPushedRef.current = false
        drawerOpenRef.current = false
        setDrawerOpen(false)
      }
    }

    function finishPhantomLocalPop() {
      if (!appLocalPopInFlightRef.current) return
      setTimeout(() => {
        const localPop = appLocalPopInFlightEntryRef.current
        appLocalPopInFlightRef.current = false
        appLocalPopInFlightEntryRef.current = null
        if (localPop?.phase === 'consume') {
          appLocalPopsRef.current = appLocalPopsRef.current.filter(
            (entry) => entry !== localPop,
          )
          consumeAppEntry(localPop.targetEntryId, localPop.ownerKey)
        }
        resumeLocalAppPops()
      }, 0)
    }

    function isConsumedAppEntry(state) {
      const id = state?.kind === 'app' ? navEntryId(state) : null
      return !!(id && consumedAppEntryIdsRef.current.has(id))
    }

    function markConsumedAppEntry(state) {
      const id = state?.kind === 'app' ? navEntryId(state) : null
      if (id) consumedAppEntryIdsRef.current.add(id)
    }

    function handleBack(destination, source) {
      backFiredRef.current = true
      setTimeout(() => { backFiredRef.current = false }, 400)
      const closeDrawerNextFrame = () => {
        if (typeof requestAnimationFrame === 'function') {
          requestAnimationFrame(() => setDrawerOpen(false))
        } else {
          setDrawerOpen(false)
        }
      }
      // (1) Drawer-first: a back that consumes the drawer's own sentinel closes
      // the drawer only — never pops navStack. Catches real back-gestures on a
      // drawer-open view AND closeDrawer's history.back().
      if (drawerOpenRef.current && drawerPushedRef.current) {
        drawerPushedRef.current = false
        drawerOpenRef.current = false
        closeDrawerNextFrame()
        appLocalPopInFlightRef.current = false
        setTimeout(resumeLocalAppPops, 0)
        return
      }
      const sourceEntryId = source?.kind === 'app' ? navEntryId(source) : null
      // (2) Consumed/retired app source: atomic semantic discard. The physical
      // traversal is real, but there is no nested level to close a second time
      // and no shell edge to pop. If the retired source is ALSO the in-flight
      // pump's target, unwedge the one global pump so a queued/drawer request can
      // still run (contract §4.2.2).
      if (isConsumedAppEntry(source)) {
        const inFlight = appLocalPopInFlightEntryRef.current
        if (inFlight && sourceEntryId && sourceEntryId === inFlight.targetEntryId) {
          appLocalPopInFlightRef.current = false
          appLocalPopInFlightEntryRef.current = null
          appLocalPopsRef.current = appLocalPopsRef.current.filter((e) => e !== inFlight)
          setTimeout(resumeLocalAppPops, 0)
        }
        return
      }
      // Resolve the popped app entry's ORIGINAL owner from the registry, else the
      // route's pane hint. Identity comes from the popped entry, never from
      // focus — React can focus another pane between history.back() and this
      // event (contract §5.3.6).
      const sourceOwnerRec = sourceEntryId ? appEntryOwnersRef.current.get(sourceEntryId) : null
      const sourceOwner = sourceOwnerRec
        || (source?.kind === 'app' && source.route && source.route.appId != null
          ? { paneId: source.route.paneId, appId: String(source.route.appId) }
          : null)
      const sourceOwnerKey = sourceOwner ? ownerKeyOf(sourceOwner.paneId, sourceOwner.appId) : null
      // (3) In-flight local pop, ONLY when the popped source carries the in-flight
      // request's owner key. An owner mismatch leaves the request queued and
      // continues into branch (4) — the reentrancy defense if a user Back lands
      // while a synthetic traversal is scheduled (contract §3.4.2).
      const inFlightPop = appLocalPopInFlightRef.current ? appLocalPopInFlightEntryRef.current : null
      if (inFlightPop && sourceOwnerKey && sourceOwnerKey === inFlightPop.ownerKey) {
        appLocalPopInFlightRef.current = false
        appLocalPopInFlightEntryRef.current = null
        if (inFlightPop.phase === 'seek') {
          // Reached a tagged entry but have not traversed the app sentinel yet.
          // Re-evaluate the current entry and continue once.
          setTimeout(resumeLocalAppPops, 0)
          return
        }
        appLocalPopsRef.current = appLocalPopsRef.current.filter((e) => e !== inFlightPop)
        consumeAppEntry(inFlightPop.targetEntryId, inFlightPop.ownerKey)
        markConsumedAppEntry(source)
        setTimeout(resumeLocalAppPops, 0)
        return
      }
      // (4) Ordinary app sentinel, routed by the popped source's own tag: forward
      // moebius:nav-back to that app's unique iframe and decrement its owner
      // count. Focus follows the app to its CURRENT pane (paneOf), but accounting
      // keeps the physical entry's ORIGINAL owner key even if the tab moved.
      if (source?.kind === 'app' && sourceOwner && sourceOwner.appId != null) {
        const ws = workspaceStateRef.current.ws
        const n = appSentinelCountsRef.current.get(sourceOwnerKey) || 0
        if (isVisibleApp(ws, sourceOwner.appId) && n > 0) {
          consumeAppEntry(sourceEntryId, sourceOwnerKey)
          const pane = paneModel.paneOf(ws, tabModel.tabKey(tabModel.makeTab('app', sourceOwner.appId)))
          if (pane && pane.id !== ws.focusedPaneId) dispatchWorkspace({ type: 'FOCUS', paneId: pane.id })
          const iframe = document.querySelector(`iframe[data-app-id="${sourceOwner.appId}"]`)
          if (iframe?.contentWindow) {
            iframe.contentWindow.postMessage({ type: 'moebius:nav-back' }, '*')
          }
          return
        }
      }
      // (5) Plain route: pop navStack and restore into the hinted (else focused)
      // pane. The route payload is the compatibility fallback for a tagged entry
      // whose in-memory stack was lost.
      drawerPushedRef.current = false
      drawerOpenRef.current = false
      setDrawerOpen(false)
      const entry = navStackRef.current.pop()
      if (entry) restoreRoute(entry)
      else if (isRestorableRoute(destination?.route)) restoreRoute(destination.route)
    }

    // Navigation API path (modern Chrome): intercept() suppresses the
    // back-forward slide on desktop and gives us a cleaner handler
    // invocation than popstate.
    if (typeof navigation !== 'undefined' && navigation.addEventListener) {
      function onNavigate(e) {
        if (e.navigationType !== 'traverse') return
        if (!e.canIntercept) return
        const destination = e.destination.getState()
        const sourceEntry = navigation.currentEntry
        const source = sourceEntry?.getState?.()
          || currentNavStateRef.current
        const direction = navTraversalDirection(source, destination, {
          currentEntryIndex: sourceEntry?.index,
          destinationEntryIndex: e.destination?.index,
        })
        const sourceRoute = snapshotRoute()
        // Phantom-entry guard: ignore a traversal landing on an UNTAGGED entry —
        // one a sandboxed app/preview iframe pushed onto the shared session
        // history. Treating it as our sentinel over-pops navStack.
        if (!isMobiusNavState(destination)) {
          if (appLocalPopInFlightEntryRef.current?.phase === 'consume') {
            markConsumedAppEntry(source)
          }
          finishPhantomLocalPop()
          return
        }
        if (direction === 'forward') {
          e.intercept({ handler() {
            currentNavStateRef.current = destination
            handleForward(destination, sourceRoute)
          } })
          return
        }
        if (direction === 'same') return
        if (direction === 'unknown') {
          e.intercept({ handler() {
            currentNavStateRef.current = destination
            restoreRoute(destination.route)
          } })
          return
        }
        // Nothing to go back to — let the browser handle it (exits PWA). Check
        // every back-target source: navStack, open drawer, any app's pending
        // sentinels, a queued local pop, or a consumed/retired app source.
        if (navStackRef.current.length === 0
            && !drawerOpenRef.current
            && !_anyAppHasSentinels(appSentinelCountsRef.current)
            && appLocalPopsRef.current.length === 0
            && !isConsumedAppEntry(source)) return
        e.intercept({ handler() {
          currentNavStateRef.current = destination
          handleBack(destination, source)
        } })
      }
      navigation.addEventListener('navigate', onNavigate)
      return () => navigation.removeEventListener('navigate', onNavigate)
    }

    // popstate fallback (Safari, older Chrome).
    function onPopState() {
      const destination = history.state
      const source = currentNavStateRef.current
      const direction = navTraversalDirection(source, destination)
      const sourceRoute = snapshotRoute()
      // Phantom-entry guard: a pop landing on an UNTAGGED entry is a phantom
      // pushed onto the shared session history by a sandboxed app/preview
      // iframe, not one of our sentinels — ignore it.
      if (!isMobiusNavState(destination)) {
        if (appLocalPopInFlightEntryRef.current?.phase === 'consume') {
          markConsumedAppEntry(source)
        }
        finishPhantomLocalPop()
        return
      }
      currentNavStateRef.current = destination
      if (direction === 'forward') {
        handleForward(destination, sourceRoute)
        return
      }
      if (direction === 'same') return
      if (direction === 'unknown') {
        restoreRoute(destination.route)
        return
      }
      if (navStackRef.current.length === 0
            && !drawerOpenRef.current
            && !_anyAppHasSentinels(appSentinelCountsRef.current)
            && appLocalPopsRef.current.length === 0
            && !isConsumedAppEntry(source)) return
      handleBack(destination, source)
    }
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  // initialNav is a stable useState value (no setter); all refs and the passed
  // dispatch/restoreRoute are stable across renders.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Keep the current physical entry self-contained. snapshotRoute() re-stamps
  // the current focused pane + content ids; a derived projection cannot be
  // repaired by a scalar setter, so this only refreshes the entry's route.
  useEffect(() => {
    if (!isMobiusNavState(history.state)) return
    const kind = history.state.kind === 'drawer' && !drawerPushedRef.current
      ? 'nav'
      : history.state.kind
    currentNavStateRef.current = updateCurrentNavEntry(snapshotRoute(), { kind })
  }, [activeView, activeChatId, activeAppId, snapshotRoute])

  // A queued close from a hidden cached app becomes safe once shell Back
  // restores that app and its sentinel to the current tagged entry.
  useEffect(() => {
    resumeLocalAppPops()
  }, [activeView, activeAppId, resumeLocalAppPops])

  // Fade back in after shell-reload.
  useEffect(() => {
    if (!shellReload) return
    document.body.style.transition = 'opacity 0.2s ease'
    document.body.style.opacity = '1'
  }, [])

  // Persist active chat id locally (compatibility mirror; the workspace blob is
  // authoritative on boot).
  useEffect(() => {
    if (activeChatId) {
      try { localStorage.setItem(ACTIVE_CHAT_KEY, activeChatId) } catch { /* ignore */ }
    }
  }, [activeChatId])

  // Persist active view + app (mirror) so a cold relaunch of the shell PWA
  // restores the app the user was on.
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
    activeAppId,
    activeChatId,
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
    retireAppHistory,
  }
}
