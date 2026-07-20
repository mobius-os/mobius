import { useState, useEffect, useRef, useCallback } from 'react'
import {
  dropPopsForEntry,
  isMobiusNavState,
  isTopmostAppEntry,
  navEntryId,
  navTraversalDirection,
  ownerKeyOf,
  pushNavEntry,
  replaceNavEntry,
  selectNavPopTarget,
  updateCurrentNavEntry,
} from '../lib/navHistory.js'
import { resolveInitialNav } from '../lib/resolveInitialNav.js'
import { drawerOpenBlockedByDrag } from '../lib/drawerLifecycle.js'
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

function sameRoute(a, b) {
  return a?.view === b?.view
    && String(a?.chatId ?? '') === String(b?.chatId ?? '')
    && String(a?.appId ?? '') === String(b?.appId ?? '')
    && String(a?.paneId ?? '') === String(b?.paneId ?? '')
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
// shell form `/shell/?app=<id-or-slug>` (or `?chat=<id>`) — this reopens
// the installed standalone PWA instead of a browser tab, because it's
// inside the manifest scope (`/shell/`). The legacy out-of-scope forms
// `/app/:id` and `/chat/:id` are still parsed for back-compat (warm taps
// on notifications already in the OS tray, or older senders).
export const deepLink = (() => {
  const path = window.location.pathname
  // In-scope cold-start form: /shell/?app=<id-or-slug> | /shell/?chat=<id>.
  if (/^\/shell\/?$/.test(path)) {
    try {
      const params = new URLSearchParams(window.location.search)
      const app = params.get('app')
      const chat = params.get('chat')
      const intent = params.get('intent')
      if (app) {
        const parsedAppId = /^\d+$/.test(app) ? parseInt(app, 10) : null
        return {
          view: 'canvas',
          app,
          appId: Number.isFinite(parsedAppId) ? parsedAppId : null,
          intent,
        }
      }
      if (chat) return { view: 'chat', chatId: chat, intent }
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
 *   replaceImplicitBootTab
 *                       true when the only boot tab is the unpinned home
 *                       surface, which an explicit deep link replaces.
 *
 * Three load-bearing pieces remain: `openDrawer` pushes one mobile sentinel;
 * `navTo` retags that sentinel or pushes one ordinary destination; every modal
 * close funnels through `history.back()` → `handleBack`'s drawer-first guard.
 * Desktop sidebar preference and visibility intentionally live outside this
 * hook in Shell/useDesktopSidebar.
 */
export default function useNavigation({
  workspace,
  workspaceStateRef,
  dispatchWorkspace,
  visiblePaneIds,
  blobValid,
  replaceImplicitBootTab,
  dragActiveRef,
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
  // Settings is the ONLY view state navigation owns globally (§1). It is the
  // full-workspace TAKEOVER overlay used in single mode (and when the builder
  // flag is off); in builder mode Settings is a pane TAB instead, so the overlay
  // stays closed and the tab drives the surface. `settingsOpen` therefore means
  // strictly "the takeover overlay is up" — NOT "the focused content is Settings"
  // (a builder tab is that without the overlay). The render tells the two apart
  // via this flag alone, never via `activeView` (design: structural separation).
  //
  // A reload/return-to-settings opens the overlay ONLY when Settings is NOT a
  // builder tab; in builder mode the persisted blob restores the Settings tab and
  // the boot effect re-opens it, so the overlay must start closed.
  const [settingsOpen, setSettingsOpen] = useState(
    initialNav.view === 'settings'
    && !(paneModel.BUILDER_SETTINGS_ENABLED && workspace.viewMode === 'panes'),
  )
  const [drawerOpen, setDrawerOpen] = useState(false)

  // ── Derived legacy triple (the projection, design §1) ────────────────────
  // World-aware (two-worlds design): the single world's slot in single mode, the
  // focused pane's active tab in builder. So the legacy triple — and everything
  // downstream (reload snapshot, deep-link resolution, immersive) — reflects what
  // the CURRENT world actually shows, and a single-world reload restores the slot,
  // not the builder focus.
  const contentRoute = paneModel.activeContentRoute(workspace)
  // The single-world takeover overlay counts only where it can PAINT — single mode
  // (or flag-off). In builder it is SUSPENDED (deriveContentVisibility gates it
  // off), so activeView reflects the tree, not the suspended flag — else a builder
  // session would report activeView:'settings' while painting the tree. Two-worlds
  // (finding 2): Settings is context-independent — a single overlay + a builder
  // tab, each shown only in its own world, NEVER destructively converted between
  // them (INV 6/7).
  const overlayShowing = settingsOpen
    && (workspace.viewMode === 'single' || !paneModel.BUILDER_SETTINGS_ENABLED)
  const activeView = overlayShowing ? 'settings' : contentRoute.view
  const activeChatId = contentRoute.chatId
  const activeAppId = contentRoute.appId

  // Guards the one-shot HOME seed against a StrictMode double-mount / any
  // remount (pushNavEntry is not idempotent). See the mount effect below.
  const seededHomeRef = useRef(false)
  const historyInitializedRef = useRef(false)

  const navStackRef = useRef([])
  // Monotonic shell-route generation. Delayed cold-boot slug resolution uses
  // this to avoid reopening an app after the user navigated elsewhere.
  const navigationEpochRef = useRef(0)
  const projectedRouteRef = useRef({ activeView, activeChatId, activeAppId })
  const projectedRoute = projectedRouteRef.current
  if (
    projectedRoute.activeView !== activeView
    || projectedRoute.activeChatId !== activeChatId
    || projectedRoute.activeAppId !== activeAppId
  ) {
    // Catch workspace-only route changes (for example, closing the active tab)
    // that do not pass through navTo/restoreRoute. The explicit increments in
    // those functions remain synchronous guards before React's next render.
    navigationEpochRef.current += 1
    projectedRouteRef.current = { activeView, activeChatId, activeAppId }
  }
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
  // A close may have to traverse untagged iframe-created entries before it
  // reaches the tagged entry beneath the drawer sentinel. Keep that traversal
  // serialized so the modal stays inert until the sentinel is truly consumed.
  const drawerClosePendingRef = useRef(false)
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
  // Forward reference to resumeLocalAppPops (defined below): retireAppHistory is
  // declared before the pump, so it re-pumps through this ref to avoid a TDZ in
  // its dependency list.
  const resumeLocalAppPopsRef = useRef(null)
  // Last tagged entry reached by the shell. popstate does not expose its source
  // entry, so this is the fallback browser's direction cursor. It deliberately
  // stays put while traversing iframe-created phantom entries; the next tagged
  // destination can then still be compared with the last shell position.
  const currentNavStateRef = useRef(null)
  // Legacy app-local levels are destructive: Back tells the iframe to close
  // them, and Forward cannot recreate them. Remember those physical entries so
  // revisiting one can safely degrade to the app base. Reversible entries use
  // their runtime correlation instead; retired entries stay consumed too.
  const consumedAppEntryIdsRef = useRef(new Set())
  // Forward into a reversible app entry is tentative until the exact runtime
  // explicitly acknowledges or rejects the matching request. This is keyed by
  // app + request id because browser/device timing is not a restoration signal.
  const pendingAppForwardsRef = useRef(new Map())
  // Resources deleted this session, as `chat:<id>` / `app:<id>` keys. The in-
  // memory navStack is scrubbed on delete, but a PHYSICAL history route payload
  // can survive; restoreRoute rejects a tombstoned target so Back/Forward cannot
  // recreate the deleted tab through the branch-(5) route fallback (§5.1.1).
  const tombstonedRouteRef = useRef(new Set())
  const tombstoneRoute = useCallback((kind, id) => {
    if (id != null) tombstonedRouteRef.current.add(`${kind}:${String(id)}`)
  }, [])

  // Visibility gate: an app is visible iff Settings is closed, the app is its
  // pane's active tab, and that pane is in the renderer's committed visible set
  // (design §5, contract §3.1.1). NOT "mounted in the cache" and NOT merely
  // "contained by a pane".
  // The pane that OWNS an app's in-app Back history + mount: its real visible tree
  // pane, or the SYNTHETIC single-world owner when the app is the current slot app
  // painted full-bleed in single mode but absent from the tree (finding 8; INV 4).
  // Returns null when the app is not currently painted anywhere.
  const appOwnerPaneId = useCallback((ws, appId) => {
    if (appId == null || settingsOpenRef.current) return null
    const key = tabModel.tabKey(tabModel.makeTab('app', appId))
    // WORLD-AWARE (finding F5): mirror deriveContentVisibility's slot derivation so
    // the nav adapter and the renderer can NEVER disagree about what paints. In
    // SINGLE mode the only painted app is the slot — the builder projection (paneOf
    // + visiblePaneIds) names HIDDEN tree panes that do not paint, so consulting it
    // there would classify an unpainted app as visible and let Back FOCUS + nav-back
    // into an invisible iframe. Only the BUILDER world reads the tree/visible set.
    const mode = paneModel.WORKSPACE_SPLITS_ENABLED ? ws.viewMode : 'single'
    if (mode === 'single') {
      if ('singleScreen' in ws) {
        const slot = ws.singleScreen
        if (slot && slot.kind === 'app' && String(slot.id) === String(appId)) {
          // Renders + pins even though paneOf() misses it (tree-absent slot), so it
          // installs history under a stable synthetic owner.
          return paneModel.SINGLE_SLOT_PANE
        }
        return null
      }
      // Legacy (absent-slot) blob: the pre-two-worlds collapse painted the focused
      // pane's active tab, so that pane is the visible owner — exactly the renderer's
      // focused-pane fallback for an uninitialized slot.
      const focused = ws.panes[ws.focusedPaneId]
      return (focused && focused.activeTabKey === key) ? ws.focusedPaneId : null
    }
    const pane = paneModel.paneOf(ws, key)
    if (pane && pane.activeTabKey === key && visiblePaneIdsRef.current.has(pane.id)) {
      return pane.id
    }
    return null
  }, [])
  const isVisibleApp = useCallback(
    (ws, appId) => appOwnerPaneId(ws, appId) != null, [appOwnerPaneId],
  )

  // Register a new live app entry: record its owner + increment its count. Both
  // structures, exactly once (contract §3.1.3).
  const addAppEntry = useCallback((entryId, paneId, appId, appNav = null) => {
    if (!entryId) return
    appEntryOwnersRef.current.set(entryId, {
      paneId: String(paneId), appId: String(appId), status: 'live', appNav,
    })
    const key = ownerKeyOf(paneId, appId)
    const m = appSentinelCountsRef.current
    m.set(key, (m.get(key) || 0) + 1)
  }, [])

  // Consume one physical app entry: decrement its owner count and flip its
  // registry status. Legacy/retired slots enter the consumed set; reversible
  // slots become dormant so Forward can revive their runtime correlation.
  // `ownerKey` is passed when the caller already resolved it (the entry may
  // have moved panes, so its ORIGINAL owner key governs accounting even when
  // focus is elsewhere).
  const consumeAppEntry = useCallback((entryId, ownerKey, appNav = null) => {
    if (!entryId) return
    const rec = appEntryOwnersRef.current.get(entryId)
    const reversible = appNav?.reversible === true || rec?.appNav?.reversible === true
    // Consumption is idempotent. A Forward→Back traversal or a synthetic/user
    // race can revisit an already-consumed physical entry; it must not decrement
    // a different still-live level owned by the same pane/app.
    if (!rec || rec.status !== 'live') {
      if (reversible && rec?.status === 'dormant') {
        consumedAppEntryIdsRef.current.delete(entryId)
      } else {
        consumedAppEntryIdsRef.current.add(entryId)
      }
      return
    }
    const key = ownerKey || (rec ? ownerKeyOf(rec.paneId, rec.appId) : null)
    if (key) {
      const m = appSentinelCountsRef.current
      const n = m.get(key) || 0
      if (n === 1) m.delete(key)
      else if (n > 1) m.set(key, n - 1)
    }
    rec.status = reversible ? 'dormant' : 'consumed'
    if (reversible) consumedAppEntryIdsRef.current.delete(entryId)
    else consumedAppEntryIdsRef.current.add(entryId)
  }, [])

  // Retire every live physical entry for an app before its iframe unmounts
  // (contract §4). Marks them consumed (so handleBack discards them atomically),
  // drops all of the app's owner-count keys, and clears ALL of its local pops —
  // queued AND in-flight (H1/M1: a lingering in-flight SEEK request would jam the
  // FIFO head forever, since isVisibleApp is false every pump). An in-flight
  // traversal's identity is kept ONLY on appLocalPopInFlightEntryRef so its
  // inevitable popstate is absorbed as a completing traversal, and its target is
  // remembered-consumed; the frame is never messaged. Then re-pump so the next
  // app's request runs. `reason` is stored on the record for diagnostics.
  // Idempotent: re-running for already-retired records is a no-op, so AppCanvas's
  // unmount cleanup can call it as a backstop.
  const retireAppHistory = useCallback((appId, reason = 'evict') => {
    const target = String(appId)
    for (const [entryId, rec] of appEntryOwnersRef.current) {
      if (rec.appId === target && rec.status !== 'retired') {
        consumedAppEntryIdsRef.current.add(entryId)
        rec.status = 'retired'
        rec.retiredReason = reason
      }
    }
    for (const [key, pending] of pendingAppForwardsRef.current) {
      if (pending.appId !== target) continue
      clearTimeout(pending.timer)
      pendingAppForwardsRef.current.delete(key)
    }
    const m = appSentinelCountsRef.current
    for (const key of [...m.keys()]) {
      try {
        const parsed = JSON.parse(key)
        if (Array.isArray(parsed) && String(parsed[1]) === target) m.delete(key)
      } catch { /* ignore a malformed key */ }
    }
    const inFlight = appLocalPopInFlightRef.current ? appLocalPopInFlightEntryRef.current : null
    // Drop every request for this app from the queue, including the in-flight one
    // (its identity survives on appLocalPopInFlightEntryRef to absorb its popstate).
    appLocalPopsRef.current = appLocalPopsRef.current.filter((req) => req.appId !== target)
    if (inFlight && inFlight.appId === target) {
      consumedAppEntryIdsRef.current.add(inFlight.targetEntryId)
    }
    setTimeout(() => resumeLocalAppPopsRef.current?.(), 0)
  }, [])

  const snapshotRoute = useCallback(() => {
    // CURRENT-WORLD route, not the builder focus: in single mode the visible
    // surface is the slot, and snapshotting the hidden builder focus corrupted
    // every single-mode Back target and same-route short-circuit (with builder
    // focus A + slot B, opening C recorded A — Back then rewrote the slot to A,
    // and re-opening A from the drawer was rejected as "same route").
    // activeContentRoute is the one world-aware projection (two-worlds design).
    const content = paneModel.activeContentRoute(workspaceStateRef.current.ws)
    // When Settings is open the physical route is a Settings route, but it
    // retains the focused content ids + pane hint (contract §2.2.1).
    const view = settingsOpenRef.current ? 'settings' : content.view
    return navRoute(view, content.chatId, content.appId, content.paneId)
  }, [workspaceStateRef])

  const pushShellEntry = useCallback((kind, route, appNav = null) => {
    const state = pushNavEntry(kind, route, {
      currentState: currentNavStateRef.current,
      appNav,
    })
    currentNavStateRef.current = state
    return state
  }, [])

  function openDrawer() {
    // Stand down while a workspace drag is live — symmetric to the Drawer's
    // swipe-CLOSE handlers (both touch and pointer). A tab dragged toward the
    // left root edge otherwise surfaces the drawer over the drop target instead
    // of splitting a left pane (owner report, live testing).
    if (drawerOpenBlockedByDrag(dragActiveRef?.current)) return
    // Synchronous guard for a rapid double activation before React has rendered
    // `drawerOpen=true`. One drawer owns exactly one physical sentinel.
    if (drawerOpenRef.current) return
    drawerClosePendingRef.current = false
    // Do not let a just-issued app history traversal consume a drawer entry
    // pushed after it began. Preserve the user's intent and open once the
    // serialized local-pop pump is idle.
    if (appLocalPopInFlightRef.current) {
      drawerOpenAfterLocalPopRef.current = true
      return
    }
    pushShellEntry('drawer', snapshotRoute())
    drawerPushedRef.current = true
    // Advance the ref synchronously so a same-batch open→close sees "open" and
    // does not leave the just-pushed sentinel dangling (§5.3.2).
    drawerOpenRef.current = true
    setDrawerOpen(true)
  }

  function closeDrawer() {
    // A modal close owns one serialized traversal. Escape, overlay, toggle, and
    // breakpoint cleanup can arrive in the same frame; a second back() would
    // skip past the drawer's sentinel before the first traversal settles.
    if (!drawerOpenRef.current || drawerClosePendingRef.current) return
    if (drawerPushedRef.current) {
      // Funnel through history.back() so handleBack handles the state
      // transition. This makes back-gesture and overlay-tap follow
      // exactly the same code path through handleBack, with the
      // drawer-first guard there preventing navStack over-pop.
      drawerClosePendingRef.current = true
      history.back()
    } else {
      // Defensive: drawer open without a sentinel (shouldn't happen
      // in normal flow). Just close it directly.
      drawerOpenRef.current = false
      drawerClosePendingRef.current = false
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
  const appNavPush = useCallback((appId, navMeta = {}) => {
    if (appId == null) return false
    const ws = workspaceStateRef.current.ws
    const ownerPaneId = appOwnerPaneId(ws, appId)
    if (ownerPaneId == null) return false
    const ownerKey = ownerKeyOf(ownerPaneId, appId)
    if ((appSentinelCountsRef.current.get(ownerKey) || 0) >= MAX_APP_SENTINELS) return false
    // Focus the visible owner pane (design §5: an app gesture focuses its pane).
    // The SYNTHETIC single-world owner has no tree pane to focus — single mode has
    // no pane focus — so skip the FOCUS for it (finding 8).
    if (ownerPaneId !== paneModel.SINGLE_SLOT_PANE) {
      dispatchWorkspace({ type: 'FOCUS', paneId: ownerPaneId })
    }
    const appNav = {
      appId: String(appId),
      requestId: typeof navMeta.requestId === 'string' ? navMeta.requestId : null,
      label: typeof navMeta.label === 'string' ? navMeta.label : null,
      reversible: navMeta.reversible === true,
    }
    let state
    try {
      state = pushShellEntry(
        'app',
        navRoute('canvas', null, Number(appId), ownerPaneId),
        appNav,
      )
    } catch { return false }
    addAppEntry(navEntryId(state), ownerPaneId, appId, appNav)
    return true
  }, [addAppEntry, appOwnerPaneId, dispatchWorkspace, pushShellEntry, workspaceStateRef])

  const appNavForwardResult = useCallback((appId, requestId, restored) => {
    if (typeof requestId !== 'string') return
    const ownerId = String(appId)
    const key = `${ownerId}:${requestId}`
    const pending = pendingAppForwardsRef.current.get(key)
    if (!pending || pending.appId !== ownerId) return
    pendingAppForwardsRef.current.delete(key)
    clearTimeout(pending.timer)

    const current = currentNavStateRef.current
    if (!restored) {
      if (navEntryId(current) === pending.entryId && current?.kind === 'app') {
        currentNavStateRef.current = updateCurrentNavEntry(
          current.route,
          { kind: 'nav', appNav: null },
        )
      }
      return
    }

    const stillCurrent = navEntryId(current) === pending.entryId && current?.kind === 'app'
    const stillUnderCurrent = Number.isInteger(current?.index)
      && Number.isInteger(pending.index)
      && current.index > pending.index
    if (!stillCurrent && !stillUnderCurrent) {
      // The owner backed out before the restoration reply arrived. Balance the
      // late runtime activation rather than keeping hidden detail state without
      // a live shell sentinel.
      const iframe = document.querySelector(`iframe[data-app-id="${ownerId}"]`)
      iframe?.contentWindow?.postMessage({
        type: 'moebius:nav-back',
        requestId,
      }, '*')
      return
    }

    const rec = appEntryOwnersRef.current.get(pending.entryId)
    if (!rec || rec.status === 'retired') {
      const iframe = document.querySelector(`iframe[data-app-id="${ownerId}"]`)
      iframe?.contentWindow?.postMessage({
        type: 'moebius:nav-back',
        requestId,
      }, '*')
      if (stillCurrent) {
        currentNavStateRef.current = updateCurrentNavEntry(
          current.route,
          { kind: 'nav', appNav: null },
        )
      }
      return
    }
    if (rec.status !== 'live') {
      rec.status = 'live'
      const ownerKey = ownerKeyOf(rec.paneId, rec.appId)
      const counts = appSentinelCountsRef.current
      counts.set(ownerKey, (counts.get(ownerKey) || 0) + 1)
    }
    consumedAppEntryIdsRef.current.delete(pending.entryId)
  }, [])

  const pumpLocalAppPop = useCallback(() => {
    if (appLocalPopInFlightRef.current) return
    if (drawerOpenRef.current) return
    // Drop any FIFO heads whose physical entry was already consumed (an ordinary
    // Back can satisfy a hidden app's queued pop directly). A consumed entry can
    // never be topmost again, so leaving it at the head would wedge the pump.
    while (appLocalPopsRef.current.length
      && consumedAppEntryIdsRef.current.has(appLocalPopsRef.current[0].targetEntryId)) {
      appLocalPopsRef.current = appLocalPopsRef.current.slice(1)
    }
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
  resumeLocalAppPopsRef.current = resumeLocalAppPops

  /** Consume one app-sentinel (e.g. user tapped the in-app back button inside
   *  the mini-app). Enqueues one FIFO request for the newest live physical entry
   *  of that app that no queued/in-flight request has already claimed, then calls
   *  the pump. No-op if the app has no such entry — which also collapses a
   *  double-tap before the first popstate to a single close (H1). */
  const appNavPop = useCallback((appId) => {
    if (appId == null) return
    const claimed = new Set(appLocalPopsRef.current.map((r) => r.targetEntryId))
    const target = selectNavPopTarget(
      [...appEntryOwnersRef.current.entries()], String(appId), claimed,
    )
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
  }, [pumpLocalAppPop])

  /** AppCanvas calls this on iframe unmount / live-frame swap. It retires the
   *  frame's physical history so orphan entries never route Back into a dead
   *  browsing context (contract §4.1.1). */
  const appNavReset = useCallback((appId) => {
    if (appId == null) return
    retireAppHistory(appId, 'reset')
  }, [retireAppHistory])

  // The ONE mode-conditional destination for Settings — and the template for any
  // future takeover-class surface (design: branch in the nav adapter, not the
  // reducer or the render). Every Settings entry point — navTo('settings'),
  // Back/Forward restore, and the reload-return boot — routes through here so the
  // tab-vs-overlay choice lives in exactly one place.
  //
  // ABSOLUTE INVARIANT (owner's words): "we only get single screens when we are in
  // single screen mode, otherwise everything we click goes into a tab." In builder
  // mode NOTHING renders full-screen — Settings, and every takeover-class surface,
  // opens as a TAB in the focused pane (existing dedup ⇒ single-instance). The
  // full-screen takeover exists ONLY in single-screen mode.
  //
  // SCOPE: DESTINATIONS, NOT DIALOGS (adjudicated, review §2). The invariant governs
  // navigable DESTINATIONS — screens you go TO (Settings, future takeover views,
  // immersive-solo). It does NOT govern transient, dismissible DIALOGS layered over
  // the workspace — the walkthrough overlay, the update-review modal (the design doc
  // keeps it fixed), chat dialogs — which are gates/modals, not screens, and each
  // carries its own dismiss. Reclassifying one of those as a destination is an
  // explicit owner call, not this invariant's job.
  //   - builder enabled + viewMode 'panes' → close the takeover overlay and open
  //     the canonical Settings tab in the target pane (dedup focuses an existing one);
  //   - single mode / flag off → today's full-screen takeover overlay.
  // Refs advance synchronously alongside the setState so a second nav in the same
  // React batch snapshots the correct overlay flag (mirrors navTo's own pattern).
  const applySettingsDestination = useCallback((paneId) => {
    const ws = workspaceStateRef.current.ws
    if (paneModel.BUILDER_SETTINGS_ENABLED && ws.viewMode === 'panes') {
      const targetPaneId = (typeof paneId === 'string' && ws.panes[paneId])
        ? paneId
        : ws.focusedPaneId
      setSettingsOpen(false)
      settingsOpenRef.current = false
      dispatchWorkspace({
        type: 'OPEN_TAB', paneId: targetPaneId, tab: tabModel.settingsTab(), activate: true,
      })
    } else {
      setSettingsOpen(true)
      settingsOpenRef.current = true
    }
  }, [dispatchWorkspace, workspaceStateRef])

  // ── The ONE navigation decision point (two-worlds design) ──────────────────
  // applySettingsDestination generalized to ALL destinations. Every path that
  // APPLIES a chat/app/settings destination — navTo, restoreRoute, boot/deep-link,
  // no-history opens, app-history restoration — funnels here, so the single-vs-
  // builder branch lives in EXACTLY ONE place and no caller can accidentally
  // OPEN_TAB into the pane tree while in single mode (design risk 4: nav bypasses).
  // It only APPLIES the destination; history construction stays in navTo/restoreRoute.
  //   Settings → applySettingsDestination (single/off = takeover overlay, builder = tab).
  //   Chat/app:
  //     single  → SET_SINGLE_SCREEN (the pane tree is NEVER touched; slot only).
  //     builder → OPEN_TAB in the hinted/focused pane (today's behavior).
  // `item` null/absent in single mode sets the empty/home screen. Returns nothing.
  const applyModeDestination = useCallback((route) => {
    if (!route || route.view === 'settings') {
      applySettingsDestination(route?.paneId)
      return
    }
    const ws = workspaceStateRef.current.ws
    // Kill-switch clamp BEFORE the world branch (finding 9; INV 16): with splits
    // disabled the presentation is single, so a chat/app nav must set the SLOT,
    // never OPEN_TAB into the hidden pane tree.
    const mode = paneModel.WORKSPACE_SPLITS_ENABLED ? ws.viewMode : 'single'
    // A chat/app destination always leaves any Settings takeover overlay.
    setSettingsOpen(false)
    settingsOpenRef.current = false
    if (mode === 'single') {
      const item = route.view === 'canvas'
        ? (route.appId != null ? { kind: 'app', id: route.appId } : null)
        : (route.chatId != null ? { kind: 'chat', id: route.chatId } : null)
      dispatchWorkspace({ type: 'SET_SINGLE_SCREEN', item })
      return
    }
    const targetPaneId = (typeof route.paneId === 'string' && ws.panes[route.paneId])
      ? route.paneId
      : ws.focusedPaneId
    const tab = route.view === 'canvas'
      ? tabModel.makeTab('app', route.appId)
      : tabModel.makeTab('chat', route.chatId)
    dispatchWorkspace({ type: 'OPEN_TAB', paneId: targetPaneId, tab, activate: true })
  }, [applySettingsDestination, dispatchWorkspace, workspaceStateRef])

  // Settings is context-independent across a world toggle (two-worlds design,
  // finding 2 / INV 6-7): the SINGLE world owns the takeover overlay (`settingsOpen`,
  // painted only in single mode), the BUILDER world owns its own Settings TAB in the
  // pane tree. Toggling worlds changes which is VISIBLE — it must NOT destructively
  // convert the overlay into a tab or CLOSE a builder Settings tab. The old
  // conversion permanently destroyed an unfocused Settings-only pane on exit (it
  // closed the tab globally, the pane collapsed, and re-entry could not reconstruct
  // it) and could restore a `single + Settings-tab` snapshot; leaving each world's
  // Settings state intact removes both. A stray Settings tab in single mode is
  // simply hidden (single paints the slot), so it is no longer a forbidden state.
  // Kept as a no-op the toggle can still call, so the call site stays uniform.
  const convertSettingsForModeTransition = useCallback(() => {}, [])

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
      const tab = tabModel.makeTab('app', appId)
      const { opts: target } = tabModel.tabNavTarget(tab)
      // Reject a malformed payload before any history write (§1.3.1): a non-finite
      // app id (tabNavTarget yields NaN) would push history for a tab the reducer
      // then rejects.
      if (target.appId == null || !Number.isFinite(target.appId)) return
      nextRoute = navRoute('canvas', null, target.appId, targetPaneId)
      openTab = tab
    } else if (view === 'chat') {
      const chatId = 'chatId' in opts ? opts.chatId : activeChatIdRef.current
      // Reject a missing/empty chat id before any history write (§1.3.1).
      if (chatId == null || String(chatId).trim() === '') return
      nextRoute = navRoute('chat', String(chatId), null, targetPaneId)
      openTab = tabModel.makeTab('chat', chatId)
    } else {
      return
    }

    // Clicking the current destination is a close/no-op, not a new semantic
    // route. This matters for a persistent desktop sidebar, where the active row
    // remains visible and repeated activations must not create dead Back steps.
    if (sameRoute(previousRoute, nextRoute)) {
      if (drawerOpenRef.current) closeDrawer()
      return
    }

    navigationEpochRef.current += 1

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

    // One reducer action makes payload+view atomic (§1.3.2). The ONE decision
    // point applies the destination to the correct world: a chat/app nav in single
    // mode sets the slot (tree untouched), in builder opens the pane tab; Settings
    // routes to tab-or-overlay. This is what stops a single-mode nav from mutating
    // the pane tree (two-worlds design). openTab is no longer needed — the route
    // carries the payload — but is still built above for the early malformed guard.
    void openTab
    applyModeDestination(nextRoute)
  }

  // Restore a route on Back/Forward. Hoisted (not defined in the mount effect)
  // and stable so the history listeners capture one identity. Settings-only
  // routes flip the overlay flag; chat/canvas routes dispatch exactly one
  // pane-targeted OPEN_TAB (workspace-wide dedup follows a moved tab to its
  // current owner) and close Settings (contract §2.3).
  const restoreRoute = useCallback((route) => {
    if (!isRestorableRoute(route)) return
    navigationEpochRef.current += 1
    if (route.view === 'settings') {
      // Same mode-conditional destination as a fresh nav: builder restores the
      // Settings tab into the route's pane hint (dedup follows a moved tab),
      // single/flag-off restores the takeover overlay.
      applySettingsDestination(route.paneId)
      return
    }
    const ws = workspaceStateRef.current.ws
    const paneId = ws.panes[route.paneId] ? route.paneId : ws.focusedPaneId
    const single = ws.viewMode === 'single'
    // Last-guard against a Back/Forward that lands on a physical route for a
    // resource deleted this session: never recreate it (§5.1.1). homeSeed chat
    // routes carry no concrete id, so they are exempt. In single mode the deleted
    // item already cleared the slot (reducer reconciliation), so degrade to the
    // empty/home screen; in builder just refocus.
    const tombstoneKey = route.view === 'canvas'
      ? (route.appId != null ? `app:${route.appId}` : null)
      : (!route.homeSeed && route.chatId != null ? `chat:${route.chatId}` : null)
    if (tombstoneKey && tombstonedRouteRef.current.has(tombstoneKey)) {
      if (single) dispatchWorkspace({ type: 'SET_SINGLE_SCREEN', item: null })
      else dispatchWorkspace({ type: 'FOCUS', paneId })
      setSettingsOpen(false)
      settingsOpenRef.current = false
      return
    }
    // Resolve the concrete destination (homeSeed → freshest active chat at
    // Back-time; a still-null semantic home stays empty). Then route through the
    // ONE decision point so single restores the slot and builder the pane tab —
    // restoreRoute no longer dispatches OPEN_TAB directly (design risk 4).
    let itemRoute = null
    if (route.view === 'canvas') {
      if (route.appId != null) itemRoute = { view: 'canvas', appId: route.appId, chatId: null, paneId: route.paneId }
    } else {
      const chatId = route.homeSeed ? lastChatIdRef.current : route.chatId
      if (chatId != null) itemRoute = { view: 'chat', chatId, appId: null, paneId: route.paneId }
    }
    if (itemRoute) {
      applyModeDestination(itemRoute)
      return
    }
    // Empty semantic home (a zero-chat install) or a stray no-app canvas route.
    if (single) {
      // The single world's home is an explicit-null slot.
      dispatchWorkspace({ type: 'SET_SINGLE_SCREEN', item: null })
    } else {
      // Builder: FOCUS alone would leave an app active in the pane, so
      // focusedContentRoute keeps projecting canvas and Back is a no-op — the
      // "can't get out of the restored app" trap. Close the active APP tab so the
      // pane projects the empty CHAT surface; the chat bootstrap then creates the
      // first chat (§2.3.3). A pane already showing chat/empty just gets focus.
      const pane = ws.panes[paneId]
      const active = pane?.tabs.find(t => tabModel.tabKey(t) === pane.activeTabKey)
      if (active && active.kind === 'app') {
        dispatchWorkspace({ type: 'CLOSE_TAB', paneId, tabKey: pane.activeTabKey })
      } else {
        dispatchWorkspace({ type: 'FOCUS', paneId })
      }
    }
    setSettingsOpen(false)
    settingsOpenRef.current = false
  }, [applyModeDestination, applySettingsDestination, dispatchWorkspace, workspaceStateRef])

  useEffect(() => {
    let bootPaneId = workspaceStateRef.current.ws.focusedPaneId
    if (!historyInitializedRef.current) {
      historyInitializedRef.current = true
      // An EXPLICIT deep link (notification tap, PWA launch-at-app) opens its
      // target into the focused pane, overriding even a valid persisted workspace
      // (§5.3.11). A shell-reload snapshot is NOT an override — a VALID workspace
      // blob already restored those tabs and is authoritative (§5.3.10), so the
      // legacy triple (initialNav) seeds ONLY when the blob is absent/invalid.
      // "focused pane empty" is NOT a proxy for that — a flat-tab fallback yields
      // a non-empty pane with no blob — so gate on `blobValid` directly. A seeded
      // chat is validated against the live list by Shell's chat-restore effect.
      const openBootTab = (tab) => {
        dispatchWorkspace(replaceImplicitBootTab
          ? { type: 'RESET_FLAT', tabs: [tab] }
          : { type: 'OPEN_TAB', paneId: bootPaneId, tab, activate: true })
        bootPaneId = workspaceStateRef.current.ws.focusedPaneId
      }
      // A deep link routes through the ONE decision point (finding 3; INV 2/4): in
      // an initialized SINGLE world it sets the slot (the requested item actually
      // appears) rather than OPEN_TAB-ing the hidden pane tree. Builder (and the
      // no-valid-blob RESET_FLAT path) keep openBootTab.
      const bootDeepLink = (route, tab) => {
        const mode = paneModel.WORKSPACE_SPLITS_ENABLED
          ? workspaceStateRef.current.ws.viewMode : 'single'
        if (mode === 'single') applyModeDestination(route)
        else openBootTab(tab)
        bootPaneId = workspaceStateRef.current.ws.focusedPaneId
      }
      if (deepLink?.view === 'canvas' && Number.isFinite(deepLink.appId)) {
        bootDeepLink(
          { view: 'canvas', appId: deepLink.appId, chatId: null, paneId: bootPaneId },
          tabModel.makeTab('app', deepLink.appId),
        )
      } else if (deepLink?.view === 'chat' && deepLink.chatId) {
        bootDeepLink(
          { view: 'chat', chatId: deepLink.chatId, appId: null, paneId: bootPaneId },
          tabModel.makeTab('chat', deepLink.chatId),
        )
      } else if (!blobValid && initialNav.view === 'canvas' && initialNav.appId != null) {
        // No valid blob: the flat-tab seed is only the tab STRIP; the legacy
        // triple (moebius_active_view/_app/_chat) names the ACTIVE tab and must
        // win as active — NOT gated on `bootPaneEmpty`, because a prior session's
        // persisted mobius-open-tabs makes the seeded pane non-empty and would
        // otherwise strand the restore on the wrong (stale) tab. openBootTab dedups
        // an already-open tab and replaces a lone implicit-home tab.
        openBootTab(tabModel.makeTab('app', initialNav.appId))
      } else if (!blobValid && initialNav.chatId != null) {
        openBootTab(tabModel.makeTab('chat', initialNav.chatId))
      } else if (
        initialNav.view === 'settings'
        && paneModel.BUILDER_SETTINGS_ENABLED
        && workspaceStateRef.current.ws.viewMode === 'panes'
      ) {
        // Reload/return-to-settings in builder mode: make the Settings tab the
        // focused surface. Idempotent when a valid blob already restored it
        // (OPEN_TAB dedups); NECESSARY when the blob was absent/invalid, where the
        // flat seed carries no Settings tab and the overlay flag started closed —
        // without this, builder return-to-settings would show nothing. Single /
        // flag-off return keeps the initial overlay flag instead.
        applySettingsDestination(bootPaneId)
        bootPaneId = workspaceStateRef.current.ws.focusedPaneId
      }

      // Reset URL to /shell/ once on mount (must match the manifest scope). The
      // deep-link path is now in workspace/Settings state, no need to keep it
      // visible. Whether to seed HOME is derived from the ACTUAL booted view — not
      // the (possibly stale) shellReload triple: seed iff we landed on a non-chat
      // surface, so a valid canvas workspace + stale chat reload still gets a home
      // seed, and a valid chat workspace + stale canvas reload does not get a dead
      // extra Back edge (§5.3.10, fixes AC10).
      const initialRoute = snapshotRoute()
      const seedHome = initialRoute.view !== 'chat'
      const baseRoute = seedHome
        ? navRoute('chat', lastChatIdRef.current, null, bootPaneId)
        : initialRoute
      currentNavStateRef.current = replaceNavEntry('base', '/shell/', baseRoute)

      // Seed HOME as the back-stack root when this load booted into a deep
      // destination (canvas/settings) so Back always reaches the chat surface.
      // The home entry carries chatId:null so it is immune to chat-delete
      // scrubbing; handleBack resolves it to the freshest active chat.
      if (seedHome && !seededHomeRef.current) {
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
      drawerClosePendingRef.current = false
      const route = destination?.route
      // A nav entry represents a shell-level transition. Back destructively
      // removed its source from navStack, so Forward rebuilds that one edge.
      // App entries are nested within their shell route, so they do not add a
      // shell edge here. Reversible ones restore through the explicit runtime
      // handshake below; legacy ones remain intentionally destructive.
      if (destination?.kind === 'nav' && isRestorableRoute(sourceRoute)) {
        navStackRef.current.push(sourceRoute)
      }
      restoreRoute(route)

      // A legacy app has no reconstruction callback. Forward lands at its base
      // route and promotes this physical slot to an ordinary shell entry, so
      // the next Back is never swallowed by a dead app sentinel.
      if (destination?.kind === 'app'
          && !destination.appNav?.reversible
          && isConsumedAppEntry(destination)) {
        const entryId = navEntryId(destination)
        if (entryId) consumedAppEntryIdsRef.current.delete(entryId)
        currentNavStateRef.current = updateCurrentNavEntry(
          route,
          { kind: 'nav', appNav: null },
        )
      }

      // A reversible entry is tentative until the retained runtime explicitly
      // says whether its closure survived. Counts/registry state are revived
      // only in appNavForwardResult after the correlated acknowledgement.
      if (destination?.kind === 'app' && destination.appNav?.reversible) {
        const ownerId = String(destination.appNav.appId ?? route?.appId ?? '')
        const requestId = destination.appNav.requestId
        const entryId = navEntryId(destination)
        if (ownerId && typeof requestId === 'string' && entryId) {
          const pendingKey = `${ownerId}:${requestId}`
          const existing = pendingAppForwardsRef.current.get(pendingKey)
          if (existing) clearTimeout(existing.timer)
          const pending = {
            appId: ownerId,
            entryId,
            index: destination.index,
            timer: null,
          }
          pending.timer = setTimeout(() => {
            if (pendingAppForwardsRef.current.get(pendingKey) !== pending) return
            pendingAppForwardsRef.current.delete(pendingKey)
            const current = currentNavStateRef.current
            if (navEntryId(current) !== entryId || current?.kind !== 'app') return
            // Transport-failure cleanup only: if a runtime restored but its ack
            // was lost, balance that activation before retiring the slot.
            const iframe = document.querySelector(`iframe[data-app-id="${ownerId}"]`)
            iframe?.contentWindow?.postMessage({
              type: 'moebius:nav-back',
              requestId,
            }, '*')
            currentNavStateRef.current = updateCurrentNavEntry(
              current.route,
              { kind: 'nav', appNav: null },
            )
          }, 5000)
          pendingAppForwardsRef.current.set(pendingKey, pending)
          setTimeout(() => {
            const iframe = document.querySelector(`iframe[data-app-id="${ownerId}"]`)
            iframe?.contentWindow?.postMessage({
              type: 'moebius:nav-forward',
              requestId,
            }, '*')
          }, 0)
        } else {
          currentNavStateRef.current = updateCurrentNavEntry(
            route,
            { kind: 'nav', appNav: null },
          )
        }
      }
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

    function continueDrawerCloseAfterPhantom() {
      if (!drawerClosePendingRef.current) return
      setTimeout(() => {
        if (drawerClosePendingRef.current) history.back()
      }, 0)
    }

    function isConsumedAppEntry(state) {
      const id = state?.kind === 'app' ? navEntryId(state) : null
      return !!(id && consumedAppEntryIdsRef.current.has(id))
    }

    function markConsumedAppEntry(state) {
      // Reversible entries are dormant after Back, not destroyed. Forward may
      // reactivate the same runtime handle and the next Back must reach it.
      if (state?.appNav?.reversible) return
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
        drawerClosePendingRef.current = false
        drawerOpenRef.current = false
        closeDrawerNextFrame()
        appLocalPopInFlightRef.current = false
        setTimeout(resumeLocalAppPops, 0)
        return
      }
      const sourceEntryId = source?.kind === 'app' ? navEntryId(source) : null
      // Back can race the explicit Forward acknowledgement. The physical
      // traversal already returns to the app base; do not pop a shell edge while
      // the runtime decides. A late positive ack is balanced by
      // appNavForwardResult with the same request id.
      const pendingForward = sourceEntryId
        ? [...pendingAppForwardsRef.current.values()].find(
          (pending) => pending.entryId === sourceEntryId,
        )
        : null
      if (source?.appNav?.reversible && pendingForward) return
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
        consumeAppEntry(inFlightPop.targetEntryId, inFlightPop.ownerKey, source?.appNav)
        // Only remember THIS entry consumed when it is actually the request's
        // target (L2) — a coincidental owner match on a different entry must not
        // tombstone an unrelated live sentinel.
        if (sourceEntryId && sourceEntryId === inFlightPop.targetEntryId) {
          markConsumedAppEntry(source)
        }
        setTimeout(resumeLocalAppPops, 0)
        return
      }
      // (4) Ordinary app sentinel, routed by the popped source's own tag: restore
      // its cached app into a visible pane when necessary, forward nav-back to
      // that app's unique iframe, and decrement its owner count. This restoration
      // is load-bearing for a direct tab close/focus change, which does not create
      // a shell nav entry above the still-live app sentinel. Accounting keeps the
      // physical entry's ORIGINAL owner key even if the tab moved.
      if (source?.kind === 'app' && sourceOwner && sourceOwner.appId != null) {
        const ws = workspaceStateRef.current.ws
        const n = appSentinelCountsRef.current.get(sourceOwnerKey) || 0
        const iframe = document.querySelector(`iframe[data-app-id="${sourceOwner.appId}"]`)
        if (iframe?.contentWindow && n > 0) {
          if (!isVisibleApp(ws, sourceOwner.appId)) {
            // Restore through the ONE decision point (finding F5), NOT a raw
            // OPEN_TAB: single mode sets the SLOT — the painted surface — so the app
            // genuinely reappears; a raw OPEN_TAB would seed the HIDDEN tree and this
            // branch would then nav-back into an iframe the user cannot see (design
            // risk 4, the funnel restoreRoute already honors but this branch did not).
            applyModeDestination({
              view: 'canvas', appId: Number(sourceOwner.appId), chatId: null, paneId: sourceOwner.paneId,
            })
          }
          consumeAppEntry(sourceEntryId, sourceOwnerKey, source.appNav)
          // This ordinary Back consumed the sentinel directly, so any queued
          // nav-pop for the SAME physical entry is now satisfied — drop it or the
          // dead request wedges the FIFO head forever (finding: FIFO wedge).
          appLocalPopsRef.current = dropPopsForEntry(appLocalPopsRef.current, sourceEntryId)
          // Re-derive the owner in the CURRENT world (post-apply): builder yields a
          // real tree pane to FOCUS; single yields SINGLE_SLOT_PANE — already the
          // painted surface — so no tree FOCUS fires. Mirrors appNavPush's handling.
          const ownerPaneId = appOwnerPaneId(workspaceStateRef.current.ws, sourceOwner.appId)
          if (ownerPaneId && ownerPaneId !== paneModel.SINGLE_SLOT_PANE
              && ownerPaneId !== workspaceStateRef.current.ws.focusedPaneId) {
            dispatchWorkspace({ type: 'FOCUS', paneId: ownerPaneId })
          }
          iframe.contentWindow.postMessage({
            type: 'moebius:nav-back',
            requestId: source.appNav?.requestId,
          }, '*')
          // Defensive re-pump (L1): a queued request behind this one can now run.
          setTimeout(resumeLocalAppPops, 0)
          return
        }
      }
      // (5) Plain route: pop navStack and restore into the hinted (else focused)
      // pane. The route payload is the compatibility fallback for a tagged entry
      // whose in-memory stack was lost.
      drawerPushedRef.current = false
      drawerClosePendingRef.current = false
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
          continueDrawerCloseAfterPhantom()
          return
        }
        // Seek reached its pinned target: clear the in-flight seek and re-pump so
        // the pump issues the consuming back() (§3.3.4). The Navigation API source
        // can be the untagged phantom's own state, which branch (3) cannot owner-
        // match — without this, execution would fall to the plain-route branch and
        // over-pop navStackRef. Keyed on the in-flight ref, not source identity.
        const inFlightSeekN = appLocalPopInFlightRef.current
          ? appLocalPopInFlightEntryRef.current : null
        if (inFlightSeekN && inFlightSeekN.phase === 'seek'
            && destination.kind === 'app'
            && navEntryId(destination) === inFlightSeekN.targetEntryId) {
          e.intercept({ handler() {
            currentNavStateRef.current = destination
            appLocalPopInFlightRef.current = false
            appLocalPopInFlightEntryRef.current = null
            setTimeout(resumeLocalAppPops, 0)
          } })
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
        continueDrawerCloseAfterPhantom()
        return
      }
      currentNavStateRef.current = destination
      // Seek that reached its pinned target on the POPSTATE path (the primary iOS
      // Safari path): popstate carries no index hints, so a back() that lands on
      // the target reads source===destination -> 'same' and would return below,
      // wedging appLocalPopInFlightRef true forever (H2). Drive seek->consume off
      // the in-flight ref instead: clear it and re-pump — the pump now sees the
      // target topmost and issues the consuming back(). (Chrome's Navigation API
      // path has index hints, reads 'back', and handles this in handleBack §3.)
      const inFlightSeek = appLocalPopInFlightRef.current
        ? appLocalPopInFlightEntryRef.current : null
      if (inFlightSeek && inFlightSeek.phase === 'seek'
          && destination.kind === 'app'
          && navEntryId(destination) === inFlightSeek.targetEntryId) {
        appLocalPopInFlightRef.current = false
        appLocalPopInFlightEntryRef.current = null
        setTimeout(resumeLocalAppPops, 0)
        return
      }
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
  // contentRoute.paneId is a dependency because moving the CURRENTLY-ACTIVE tab
  // across panes changes only the owning pane — activeView/chatId/appId stay the
  // same item — so without it the current entry keeps the OLD owner hint and Back
  // targets the wrong pane (finding: restamp misses pane-only changes).
  useEffect(() => {
    if (!isMobiusNavState(history.state)) return
    const kind = history.state.kind === 'drawer' && !drawerPushedRef.current
      ? 'nav'
      : history.state.kind
    currentNavStateRef.current = updateCurrentNavEntry(snapshotRoute(), { kind })
  }, [activeView, activeChatId, activeAppId, contentRoute.paneId, snapshotRoute])

  // A queued close from a hidden cached app becomes safe once shell Back restores
  // that app and its sentinel to the current tagged entry — or once a split/focus
  // change makes the app visible in a pane (L3: visiblePaneIds gates isVisibleApp,
  // so a parked pop whose owner just became visible must re-pump).
  useEffect(() => {
    resumeLocalAppPops()
  }, [activeView, activeAppId, visiblePaneIds, resumeLocalAppPops])

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
    // Strictly "the full-workspace takeover overlay is up" — NOT "focused content
    // is Settings" (a builder tab is the latter without the overlay). The render
    // gates pane suppression on THIS, never on activeView, so builder Settings
    // never hides sibling panes (design: the named risk, made structural).
    settingsOverlayOpen: overlayShowing,
    // The RAW suspended overlay intent (finding F3), NOT gated by the committed
    // world. Settings is context-independent across a world toggle (INV 6/7): the
    // takeover is SUSPENDED (overlayShowing false) in builder but `settingsOpen`
    // survives, so Shell mounts SettingsView on THIS and only PAINTS it on the
    // effective-gated flag — the component stays mounted-hidden across world flips
    // (mount-identity rule, like the slot chat), never torn down mid-flip.
    settingsOpenRaw: settingsOpen,
    openDrawer,
    closeDrawer,
    navTo,
    applyModeDestination,
    convertSettingsForModeTransition,
    backFiredRef,
    drawerPushedRef,
    drawerOpenRef,
    navStackRef,
    navigationEpochRef,
    activeViewRef,
    activeChatIdRef,
    activeAppIdRef,
    appNavPush,
    appNavPop,
    appNavReset,
    appNavForwardResult,
    retireAppHistory,
    tombstoneRoute,
  }
}
