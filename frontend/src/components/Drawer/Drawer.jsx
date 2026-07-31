import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useQueryClient } from '@tanstack/react-query'
import { EmptyMessage } from '@openai/apps-sdk-ui/components/EmptyMessage'
import { api } from '../../api/client.js'
import { appQueries, chatQueries } from '../../hooks/queries.js'
import {
  AppsNavIcon,
  NewChatNavIcon,
  SearchNavIcon,
  SettingsNavIcon,
} from '../navigationIcons.js'
import { appIconUrl } from '../appIcon.js'
import { computePinnedDrag } from './pinnedReorder.js'
import {
  drawerCloseWatchdogMs,
  drawerWidthFromPointerDelta,
  isGeneratedTouchClick,
  isHorizontalDrawerSwipe,
  shouldSuppressDrawerSwipeClick,
  clearDrawerGestureStyles,
} from '../../lib/drawerLifecycle.js'
import { requestSearchReveal } from '../ChatView/searchReveal.js'
import { termsFromSnippet } from '../ChatView/searchTermHighlight.js'
import { WORKSPACE_SPLITS_ENABLED } from '../Shell/paneModel.js'
import { DRAWER_HOLD_MS, PRE_HOLD_MOVE_PX } from '../Shell/dragController.js'
import InstallSheet from './InstallSheet.jsx'
import AppsDirectory from './AppsDirectory.jsx'
import DrawerItemActionMenu from './DrawerItemActionMenu.jsx'
import {
  appInitials,
  buildDrawerSections,
  filterInstalledApps,
} from './drawerInformationArchitecture.js'
import ShareAppSheet from './ShareAppSheet.jsx'
import { isDrawerAppShareEligible } from './appShareState.js'
import {
  clampDrawerRowCount,
  drawerRowCountToReveal,
  initialDrawerRowCount,
  nextDrawerRowCount,
} from './drawerProgressiveRows.js'
import {
  clampDesktopSidebarWidth,
  DESKTOP_SIDEBAR_DEFAULT_WIDTH,
  DESKTOP_SIDEBAR_MAX_WIDTH,
  DESKTOP_SIDEBAR_MIN_WIDTH,
} from '../Shell/useDesktopSidebar.js'
import './Drawer.css'

// Module-level constant so default Set props are stable across renders.
// A fresh `new Set()` per call would break identity-based memoization
// downstream.
const EMPTY_SET = new Set()

// Search snippets arrive with private-use sentinels (U+E000 / U+E001) around
// each matched word — characters that cannot occur in real transcript text —
// so highlighting needs no HTML from the server. This is the one place that
// converts them, into <mark> elements.
function SearchSnippet({ text }) {
  if (!text) return null
  const nodes = []
  text.split('\ue000').forEach((chunk, i) => {
    if (i === 0) {
      if (chunk) nodes.push(chunk)
      return
    }
    const end = chunk.indexOf('\ue001')
    if (end === -1) {
      nodes.push(chunk)
      return
    }
    nodes.push(<mark key={i}>{chunk.slice(0, end)}</mark>)
    const rest = chunk.slice(end + 1)
    if (rest) nodes.push(rest)
  })
  return <span className="drawer__result-snippet">{nodes}</span>
}

export default function Drawer({
  open,
  persistent = false,
  width = DESKTOP_SIDEBAR_DEFAULT_WIDTH,
  onWidthChange,
  interactionLocked = false,
  onClose,
  apps,
  appsStatus = 'success',
  onRetryApps,
  activeView,
  activeAppId,
  chats,
  chatsStatus = 'success',
  onRetryChats,
  activeChatId,
  onChat,
  onApp,
  onNewChat,
  onDeleteChat,
  onDeleteApp,
  onDeleteAppData,
  onSettings,
  appsActive = false,
  onAppsOpen,
  appsHost,
  // Set of chat ids whose agent is currently streaming. Used to
  // show a small accent dot next to the row label so the user can
  // see at a glance which background builds are still running.
  // Sourced from Shell (the only place that knows when a turn is
  // active across the whole app). Defaults to an empty Set so the
  // drawer renders cleanly if no parent supplies the prop.
  streamingChatIds,
  // Set of chat ids whose latest background run finished while the
  // user was elsewhere. Rendered as a green attention dot, distinct by
  // colour from the accent streaming dot above (neither animates).
  attentionChatIds,
  // Set of app ids that first appeared in the fetched list this session
  // (freshly built or App-Store-installed). Rendered as the same green
  // dot as chat attention, cleared by Shell when the app is opened —
  // an arrival cue before normal open recency takes over.
  newAppIds,
  // Truthy when local provider credentials are missing or their status could
  // not be checked. Drives a small warning dot on Settings.
  settingsWarning,
  // Shared flag the workspace drag controller raises while a row is being
  // dragged OUT of the drawer (design §3.1). The swipe-to-close touch handlers
  // below consult it and STAND DOWN so the native non-passive pan recognizer
  // never claims a gesture that the pointer-captured row drag owns. Null when
  // the flag is off.
  dragActiveRef,
}) {
  const streamingSet = streamingChatIds || EMPTY_SET
  const attentionSet = attentionChatIds || EMPTY_SET
  const newAppSet = newAppIds || EMPTY_SET
  const resizeRef = useRef(null)
  const {
    pinned: pinnedItems,
    recents: allRecents,
    apps: sortedApps,
  } = useMemo(() => buildDrawerSections(chats, apps), [chats, apps])
  const [visibleRecentCount, setVisibleRecentCount] = useState(
    () => initialDrawerRowCount(allRecents.length),
  )
  const navigationScrollRef = useRef(null)
  const recentSentinelRef = useRef(null)
  const revealedActiveChatRef = useRef(null)
  const visibleRecents = useMemo(
    () => allRecents.slice(0, visibleRecentCount),
    [allRecents, visibleRecentCount],
  )

  // Preserve the revealed window across recency reorders. Only clamp when
  // deletion/reconciliation makes the list shorter, and always keep the first
  // batch available. Resetting to one batch on every query-cache refresh would
  // make rows disappear beneath someone who was already scrolling.
  useEffect(() => {
    setVisibleRecentCount(current => clampDrawerRowCount(
      current,
      allRecents.length,
    ))
  }, [allRecents.length])

  // Selecting a chat from a workspace tab should reveal that same chat in the
  // drawer. Older chats may sit outside the progressively mounted window, so
  // first grow the window through the target row, then reveal it before paint.
  // Remember the completed reveal so recency refreshes cannot fight a later
  // manual scroll; leaving the chat or closing the drawer arms the next reveal.
  useLayoutEffect(() => {
    if (!open || activeView !== 'chat' || activeChatId == null) {
      revealedActiveChatRef.current = null
      return
    }

    const chatId = String(activeChatId)
    if (revealedActiveChatRef.current === chatId) return

    const isPinned = pinnedItems.some(({ kind, item }) => (
      kind === 'chat' && String(item.id) === chatId
    ))
    const recentIndex = allRecents.findIndex(({ kind, item }) => (
      kind === 'chat' && String(item.id) === chatId
    ))
    if (!isPinned && recentIndex < 0) return

    if (recentIndex >= visibleRecentCount) {
      setVisibleRecentCount(current => drawerRowCountToReveal(
        current,
        allRecents.length,
        recentIndex,
      ))
      return
    }

    const activeRow = [...(navigationScrollRef.current
      ?.querySelectorAll('[data-drawer-key]') || [])]
      .find(row => row.dataset.drawerKey === `chat:${chatId}`)
    if (!activeRow) return

    activeRow.scrollIntoView({ block: 'nearest', inline: 'nearest' })
    revealedActiveChatRef.current = chatId
  }, [
    activeChatId,
    activeView,
    allRecents,
    open,
    pinnedItems,
    visibleRecentCount,
  ])

  // One continuous list, progressively materialized as its sentinel nears the
  // viewport. Chat summaries and app metadata are already small navigation
  // projections; this boundary avoids mounting hundreds of interactive menu
  // trees before they can be seen. Browsers without IntersectionObserver keep
  // the fully-rendered behavior rather than making history unreachable.
  useEffect(() => {
    if (!open || visibleRecentCount >= allRecents.length) return undefined
    const root = navigationScrollRef.current
    const sentinel = recentSentinelRef.current
    if (!root || !sentinel) return undefined
    if (typeof IntersectionObserver === 'undefined') {
      setVisibleRecentCount(allRecents.length)
      return undefined
    }
    const observer = new IntersectionObserver(entries => {
      if (!entries.some(entry => entry.isIntersecting)) return
      setVisibleRecentCount(current => nextDrawerRowCount(
        current,
        allRecents.length,
      ))
    }, {
      root,
      // Reveal the next rows before the sentinel itself reaches the fade.
      rootMargin: '0px 0px 320px 0px',
    })
    observer.observe(sentinel)
    return () => observer.disconnect()
  }, [allRecents.length, open, visibleRecentCount])

  // Drawer-wide chat search. While a query is active the navigation scroll
  // (Apps / Pinned / Recents) is swapped for ranked results; clearing the
  // field restores it untouched. Keystrokes are debounced and the previous
  // request aborted, so at most one search is ever in flight.
  const [searchActive, setSearchActive] = useState(false)
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState([])
  const [searchStatus, setSearchStatus] = useState('idle') // idle|loading|success|error
  const searchInputRef = useRef(null)
  const searchTerm = searchQuery.trim()

  useEffect(() => {
    if (!searchActive || !searchTerm) {
      setSearchResults([])
      setSearchStatus('idle')
      return undefined
    }
    const ctrl = new AbortController()
    setSearchStatus('loading')
    const timer = setTimeout(async () => {
      try {
        const res = await api.chats.search(searchTerm, { signal: ctrl.signal })
        if (!res.ok) throw new Error(`search failed: ${res.status}`)
        const data = await res.json()
        if (ctrl.signal.aborted) return
        setSearchResults(Array.isArray(data) ? data : [])
        setSearchStatus('success')
      } catch {
        if (ctrl.signal.aborted) return
        setSearchStatus('error')
      }
    }, 200)
    return () => {
      clearTimeout(timer)
      ctrl.abort()
    }
  }, [searchActive, searchTerm])

  const closeSearch = useCallback(() => {
    setSearchActive(false)
    setSearchQuery('')
  }, [])

  // The input mounts on activation; focus it there so the phone keyboard
  // rises with the same tap that opened search.
  useEffect(() => {
    if (searchActive) searchInputRef.current?.focus()
  }, [searchActive])

  // A dismissed modal drawer (phone) should reopen in its normal state, not
  // mid-search. The persistent desktop sidebar keeps search across blurs.
  useEffect(() => {
    if (!open && !persistent) closeSearch()
  }, [open, persistent, closeSearch])

  // One row at a time can be in rename or open-menu mode. Tracking the
  // active id (rather than per-row state) lets a click on another row's
  // context action replace any open menu without a global listener per row.
  const [openMenu, setOpenMenu] = useState(null) // { kind, id, surface, placement } | null
  // Belt-and-braces orphan cleanup: if the row whose menu was open
  // disappears from the list (delete, chat soft-delete, agent-side
  // removal), openMenu would still reference a dead id and the next
  // row to occupy that slot can look "pressed". Drop the reference
  // the moment its id is no longer in the relevant collection.
  useEffect(() => {
    if (!openMenu) return
    const collection = openMenu.kind === 'chat' ? (chats || []) : (apps || [])
    const stillThere = collection.some(item => item.id === openMenu.id)
    if (!stillThere) setOpenMenu(null)
  }, [openMenu, chats, apps])
  const [renamingState, setRenamingState] = useState(null) // { kind, id } | null
  // Mirrors `renaming` synchronously (not via useEffect — that's one render
  // behind) so outside-tap cancellation sees the current edit immediately.
  const renamingRef = useRef(null)
  const overlayCancelRef = useRef(false)
  const renaming = renamingState
  const setRenaming = useCallback((next) => {
    renamingRef.current = next
    setRenamingState(next)
  }, [])
  // The app whose "Add to home screen" sheet is open,
  // or null. Mirrors openMenu/renamingState — one at a time, owned here
  // rather than in Shell so this stays drawer-local.
  const [installingApp, setInstallingApp] = useState(null)
  const [appQuery, setAppQuery] = useState('')
  const appsButtonRef = useRef(null)
  const filteredApps = useMemo(
    () => filterInstalledApps(sortedApps, appQuery),
    [sortedApps, appQuery],
  )

  const resetAppsSurfaceUi = useCallback(({ restoreFocus = true } = {}) => {
    setAppQuery('')
    setOpenMenu(null)
    setRenaming(null)
    if (restoreFocus) {
      requestAnimationFrame(() => appsButtonRef.current?.focus())
    }
  }, [setRenaming])

  function openApps() {
    setAppQuery('')
    setOpenMenu(null)
    setRenaming(null)
    onAppsOpen?.()
  }
  // Smart Share is also drawer-local. It consumes the same apps snapshot so
  // it can route unpublished apps through Contribute or the App Store.
  const [sharingApp, setSharingApp] = useState(null)

  // The install sheet navigates the whole document away to the standalone
  // install surface (/apps/<slug>/?install=1). When the user comes back via the
  // OS back button the browser can restore THIS document from BFCache with the
  // sheet still mounted in its mid-submit ("Saving…") state — effects don't
  // re-run on a BFCache restore, so without this the full-screen modal masks the
  // drawer undismissably and reappears on every back press. Returning means the
  // install interaction is over, so close the sheet on any page re-show.
  useEffect(() => {
    function closeOnReshow() { setInstallingApp(null) }
    window.addEventListener('pageshow', closeOnReshow)
    return () => window.removeEventListener('pageshow', closeOnReshow)
  }, [])

  // Rows receive one stable action surface instead of a new closure for every
  // action on every item. The ref supplies the latest props and local helpers,
  // while memoized rows can bail out when an unrelated row or drawer state changes.
  const rowActionInputsRef = useRef(null)
  const rowActions = useMemo(() => ({
    select(kind, id) {
      const current = rowActionInputsRef.current
      current.resetAppsSurfaceUi({ restoreFocus: false })
      if (kind === 'chat') current.onChat(id)
      else current.onApp(id)
    },
    toggleMenu(kind, id, next, surface = 'drawer', placement = null) {
      rowActionInputsRef.current.setOpenMenu(next ? {
        kind,
        id,
        surface,
        placement,
      } : null)
    },
    startRename(kind, id, surface = 'drawer') {
      rowActionInputsRef.current.setRenaming({ kind, id, surface })
    },
    cancelRename() {
      rowActionInputsRef.current.setRenaming(null)
    },
    submitRename(kind, id, previous, next) {
      const current = rowActionInputsRef.current
      current.setRenaming(null)
      if (current.overlayCancelRef.current) {
        current.overlayCancelRef.current = false
        return
      }
      if (!next || next === previous) return
      if (kind === 'chat') current.renameChat(id, next)
      else current.renameApp(id, next)
    },
    pin(kind, id, next) {
      const current = rowActionInputsRef.current
      if (kind === 'chat') current.pinChat(id, next)
      else current.pinApp(id, next)
    },
    reorderPinned(orderedKeys) {
      rowActionInputsRef.current.reorderPinned(orderedKeys)
    },
    remove(kind, id) {
      const current = rowActionInputsRef.current
      if (kind === 'chat') current.onDeleteChat(id)
      else current.onDeleteApp?.(id)
    },
    removeData(id) {
      rowActionInputsRef.current.onDeleteAppData?.(id)
    },
    install(app) {
      rowActionInputsRef.current.setInstallingApp(app)
    },
    share(app) {
      rowActionInputsRef.current.setSharingApp(app)
    },
  }), [])

  function handleOverlayPointerDown(e) {
    // Pointerdown is the canonical dismiss event. Waiting for `click` is
    // unreliable on touchscreens: even a tiny pan can suppress the synthetic
    // click, leaving the drawer visibly open while the gesture scrolls the
    // content underneath. Primary pointerdown both acknowledges the outside
    // tap immediately and pairs with the scrim's touch-action:none contract.
    if (e.button !== 0 || !e.isPrimary) return
    if (renamingRef.current || overlayCancelRef.current) {
      // Defensive fallback for browsers that reach the overlay before the
      // rename row's document-capture listener. Cancel the rename and let this
      // same outside gesture close the drawer; one tap should never be needed
      // merely to clear an intermediate drawer state.
      overlayCancelRef.current = true
      renamingRef.current = null
      setRenaming(null)
    }
    e.preventDefault()
    onClose?.()
  }

  const queryClient = useQueryClient()

  function refreshChats() {
    chatQueries.list.invalidate(queryClient)
  }
  function refreshApps() {
    appQueries.list.invalidate(queryClient)
  }

  async function renameChat(id, title) {
    const res = await api.chats.update(id, { title })
    if (res.ok) refreshChats()
  }

  async function renameApp(id, name) {
    const res = await api.apps.update(id, { name })
    if (res.ok) refreshApps()
  }

  async function pinChat(id, pinned) {
    // Optimistic: stamp/clear pinned_at locally so the row reorders the
    // instant you tap — the sort and the row's pin badge both key off
    // pinned_at. Without this the row only moves after the PATCH + refetch
    // round-trips, which reads as "nothing happened, then it did."
    // Reconcile with the server on success; roll back on failure.
    const key = chatQueries.keys.all
    const prev = queryClient.getQueryData(key)
    queryClient.setQueryData(key, (list) =>
      (list || []).map((c) =>
        c.id === id
          ? { ...c, pinned_at: pinned ? new Date().toISOString() : null }
          : c,
      ),
    )
    try {
      const res = await api.chats.update(id, { pinned })
      if (res.ok) refreshChats()
      else queryClient.setQueryData(key, prev)
    } catch {
      queryClient.setQueryData(key, prev)
    }
  }

  async function pinApp(id, pinned) {
    const key = appQueries.keys.all
    const prev = queryClient.getQueryData(key)
    queryClient.setQueryData(key, (list) =>
      (list || []).map((a) =>
        a.id === id
          ? { ...a, pinned_at: pinned ? new Date().toISOString() : null }
          : a,
      ),
    )
    try {
      const res = await api.apps.update(id, { pinned })
      if (res.ok) refreshApps()
      else queryClient.setQueryData(key, prev)
    } catch {
      queryClient.setQueryData(key, prev)
    }
  }

  // Persist a new order for the one combined pinned list (chats AND apps share
  // it). `orderedKeys` is the desired top → bottom order as "kind:id" strings —
  // exactly what the drag previewed, so the optimistic state matches pixel for
  // pixel and nothing re-shuffles when the PATCHes land.
  async function reorderPinned(orderedKeys) {
    if (!Array.isArray(orderedKeys) || orderedKeys.length === 0) return
    const chatKey = chatQueries.keys.all
    const appKey = appQueries.keys.all
    const prevChats = queryClient.getQueryData(chatKey)
    const prevApps = queryClient.getQueryData(appKey)
    // Ascending synthetic pinned_at (top oldest → bottom newest) matching the
    // rendered order; pinned_at is the ordering primitive the drawer sorts on.
    const rank = new Map()
    const now = Date.now()
    orderedKeys.forEach((key, index) => {
      rank.set(key, new Date(now + index).toISOString())
    })
    const applyRank = (kind) => (list) =>
      (list || []).map(item => rank.has(`${kind}:${item.id}`)
        ? { ...item, pinned_at: rank.get(`${kind}:${item.id}`) }
        : item)
    queryClient.setQueryData(chatKey, applyRank('chat'))
    queryClient.setQueryData(appKey, applyRank('app'))
    try {
      // Persist the order by re-stamping pin times top → bottom (each later call
      // is newer, so the bottom row ends newest — the ascending order rendered).
      // These are the QUIET `repin` calls: they do NOT invalidate the shell list,
      // and we deliberately do NOT refetch afterwards. The optimistic cache above
      // already holds the exact order the drag previewed; refetching would swap
      // client-clock stamps for server-clock ones item-by-item and visibly
      // re-shuffle the list. A later natural refetch returns the same order.
      for (const key of orderedKeys) {
        const sep = key.indexOf(':')
        const kind = key.slice(0, sep)
        const rawId = key.slice(sep + 1)
        const res = kind === 'chat'
          ? await api.chats.repin(rawId)
          : await api.apps.repin(Number(rawId))
        if (!res.ok) throw new Error('Could not reorder pinned items')
      }
    } catch {
      queryClient.setQueryData(chatKey, prevChats)
      queryClient.setQueryData(appKey, prevApps)
    }
  }

  // deleteApp is handled by Shell (where showToast lives) — the local
  // implementation silently swallowed 409 and network errors. Calls are
  // forwarded via the onDeleteApp prop; the local function is removed.

  // Focus management: move focus into the drawer on open; restore to
  // the toggle on close. The drawer panel gets tabIndex=-1 so it can
  // receive programmatic focus without appearing in the tab order.
  // previousFocusRef records the element that was focused when the
  // drawer opened so we can restore it on close regardless of how the
  // drawer was dismissed (Escape, overlay tap, swipe).
  const previousFocusRef = useRef(null)
  const drawerRef = useRef(null)
  useEffect(() => {
    if (persistent) {
      previousFocusRef.current = null
      return
    }
    if (open) {
      previousFocusRef.current = document.activeElement
      // Defer to next frame so the drawer's CSS transition has begun
      // and the panel is in the rendered DOM before we focus it.
      const focusFrame = requestAnimationFrame(() => {
        // Focus the panel itself (tabIndex=-1): the drawer keeps no dedicated
        // close control by owner decision — the scrim tap, the brand toggle,
        // and Back all close it, and focus restore on close (below) returns
        // keyboard users to wherever they came from.
        drawerRef.current?.focus()
      })
      return () => cancelAnimationFrame(focusFrame)
    } else {
      // Restore focus when the drawer closes so keyboard users land
      // back on the toggle that opened it (or whatever was focused).
      if (previousFocusRef.current && typeof previousFocusRef.current.focus === 'function') {
        previousFocusRef.current.focus()
        previousFocusRef.current = null
      }
    }
  }, [open, persistent])

  // Escape key closes the drawer while it is open. Apps is ordinary workspace
  // content, so it never takes ownership away from navigation layered above it.
  useEffect(() => {
    if (!open || persistent || interactionLocked) return
    function onKeyDown(e) {
      if (e.key === 'Escape') {
        // A row-owned menu is the topmost surface. Its own Escape handler
        // closes it and restores focus to the row; only a second Escape should
        // dismiss the mobile drawer underneath.
        if (openMenu) return
        e.stopPropagation()
        onClose?.()
      }
    }
    document.addEventListener('keydown', onKeyDown, { capture: true })
    return () => document.removeEventListener('keydown', onKeyDown, { capture: true })
  }, [open, persistent, interactionLocked, onClose, openMenu])

  // Swipe-left-to-close. Mirror of the mobius-design-iter pattern:
  // touchstart captures origin, touchmove drags the panel 1:1 with
  // the finger when the gesture is dominantly horizontal-left,
  // touchend either closes (≥70px past origin AND horizontal-
  // dominant) or snaps back. The CSS transition is disabled mid-
  // drag via `drawer--dragging` so the panel tracks the finger
  // without easing.
  const dragStart = useRef(null) // { x, y } or null
  // True once a touch gesture has become a HORIZONTAL swipe. Vertical movement
  // belongs to the drawer's scroll containers and must never arm click
  // suppression: some mobile browsers do not emit a synthetic click after a
  // scroll, so the old "any movement" rule left the suppressor waiting and ate
  // the user's next real tap on a chat/app row.
  // These handlers are bound as NATIVE listeners (see the effect below), with
  // touchmove non-passive, because the gesture must be claimed from the browser
  // rather than merely observed. React's own onTouch* props are registered
  // passive at the root, so preventDefault() from them is a no-op — and
  // `touch-action: pan-y` cannot cover for that on iOS, where WebKit still does
  // not implement the pan-* / pinch-zoom keywords (WebKit bug 133112). On an
  // iPhone the declaration is dropped entirely, the surrounding vertical
  // scroller wins the horizontal drag, and the panel never follows the finger:
  // swipe-to-close was unreachable there while working on Chrome.
  //
  // Click suppression stays as a second line of defence. Cancelling touchmove
  // suppresses the compatibility click in every engine we target, but only for
  // gestures we actually claimed; a horizontal flick classified at the very last
  // move can still emit a click on the row the finger lifted over, which would
  // select a chat/app the user only meant to swipe past.
  const swipingRef = useRef(false)
  // True once THIS gesture has been claimed as a leftward panel pan. Sticky for
  // the rest of the gesture: the finger may drift back rightward, but ownership
  // must not flip mid-drag or the browser would resume scroll arbitration
  // halfway through. Read by the non-passive touchmove listener below to decide
  // whether to preventDefault.
  const panningRef = useRef(false)
  // A component-owned guard for the click generated by THIS completed
  // horizontal swipe. This replaces the old temporary DOM listener whose
  // meaning was "swallow whatever click happens next". If the browser emits no
  // compatibility click, the next pointer/touch/key start clears this flag
  // before that later real interaction can activate a destination.
  const suppressGeneratedClickRef = useRef(false)
  const clickSuppressTimerRef = useRef(null)

  function clearClickSuppression() {
    suppressGeneratedClickRef.current = false
    if (clickSuppressTimerRef.current !== null) {
      clearTimeout(clickSuppressTimerRef.current)
      clickSuppressTimerRef.current = null
    }
  }

  function armGeneratedClickSuppression() {
    clearClickSuppression()
    suppressGeneratedClickRef.current = true
    clickSuppressTimerRef.current = setTimeout(clearClickSuppression, 400)
  }

  function onDrawerClickCapture(e) {
    if (interactionLocked) {
      e.stopPropagation()
      e.preventDefault()
      return
    }
    if (!suppressGeneratedClickRef.current) return
    const generatedByTouch = isGeneratedTouchClick(e)
    clearClickSuppression()
    if (!generatedByTouch) return
    e.stopPropagation()
    e.preventDefault()
  }

  // The panel's swipe position is an imperative, frame-by-frame DOM write.
  // React's `open` prop remains authoritative: if navigation closes the drawer
  // before the browser delivers touchend/touchcancel, clear that stale write in
  // a layout effect (before paint). Otherwise the inline translateX can keep a
  // now-inert drawer visibly stranded over the live app forever.
  useLayoutEffect(() => {
    if (open && !persistent) return
    clearClickSuppression()
    clearDrawerGestureStyles(drawerRef.current)
    dragStart.current = null
    swipingRef.current = false
    panningRef.current = false
  }, [open, persistent])

  // Keep the scrim hit-testable while the panel is sliding away. `open` flips
  // false at the START of the 250ms close transition; dropping pointer-events
  // in that same render exposes the chat/app while drawer pixels are still on
  // screen. transitionend is the normal release, with a timeout fallback for
  // reduced-motion or an interrupted compositor transition.
  const [scrimBlocking, setScrimBlocking] = useState(open && !persistent)
  useLayoutEffect(() => {
    if (open && !persistent) setScrimBlocking(true)
    if (persistent) setScrimBlocking(false)
  }, [open, persistent])
  useLayoutEffect(() => {
    if (open || !scrimBlocking) return undefined
    const panel = drawerRef.current
    const watchdogMs = panel
      ? drawerCloseWatchdogMs(getComputedStyle(panel))
      : 0
    if (watchdogMs === 0) {
      setScrimBlocking(false)
      return undefined
    }
    const timer = setTimeout(() => setScrimBlocking(false), watchdogMs)
    return () => clearTimeout(timer)
  }, [open, scrimBlocking])

  function handleDrawerTransitionEnd(e) {
    if (open || e.target !== e.currentTarget || e.propertyName !== 'transform') return
    const width = e.currentTarget.getBoundingClientRect().width
    const x = new DOMMatrixReadOnly(getComputedStyle(e.currentTarget).transform).m41
    if (x > -width + 1) return
    setScrimBlocking(false)
  }

  function onTouchStart(e) {
    // Usually pointerdown already cleared this. Keep the touch-level clear for
    // WebKit and for touch-only browser regression sequences.
    clearClickSuppression()
    if (!open || persistent || interactionLocked || e.touches.length !== 1) return
    // A row is being lifted out of the drawer — the drag controller owns this
    // gesture; swipe-to-close stands down (design §3.1).
    if (dragActiveRef?.current) { dragStart.current = null; return }
    swipingRef.current = false
    panningRef.current = false
    dragStart.current = { x: e.touches[0].clientX, y: e.touches[0].clientY }
  }
  function onTouchMove(e) {
    // Stand down mid-gesture too: a hold that armed the controller after
    // touchstart must not also pan the panel. The controller owns its own
    // pointer stream; releasing here without cancelling leaves the gesture to it.
    if (dragActiveRef?.current) { panningRef.current = false; return }
    if (!dragStart.current || e.touches.length !== 1) return
    const dx = e.touches[0].clientX - dragStart.current.x
    const dy = e.touches[0].clientY - dragStart.current.y
    const isHorizontalSwipe = isHorizontalDrawerSwipe(dx, dy)
    // Only custom horizontal gestures need the one-shot click suppressor.
    // Native vertical scrolling already owns its tap/click cancellation; arming
    // our suppressor for it made a quick post-scroll destination tap look dead.
    if (isHorizontalSwipe) {
      swipingRef.current = true
    }
    if (dx < 0 && isHorizontalSwipe) panningRef.current = true
    if (!panningRef.current) return
    // Claim the gesture. This is the whole point of the native non-passive
    // binding: cancelling the touchmove is what stops the surrounding scroller
    // (and, on iOS, WebKit's UI-process scroll arbitration, which would
    // otherwise take the drag over and deliver touchcancel) from stealing a pan
    // the drawer has already recognized as its own.
    e.preventDefault()
    const el = drawerRef.current
    if (!el) return
    el.classList.add('drawer--dragging')
    // Clamp to [-320, 0]: a finger that drifts back past the origin must not
    // drag an already-open panel further right than open.
    el.style.transform = `translateX(${Math.min(0, Math.max(dx, -320))}px)`
  }
  function onTouchEnd(e) {
    // If the controller took over, it owns pointerup too — do nothing here.
    if (dragActiveRef?.current) { dragStart.current = null; return }
    if (!dragStart.current) return
    const t = e.changedTouches[0]
    const dx = t.clientX - dragStart.current.x
    const dy = t.clientY - dragStart.current.y
    const shouldClose = dx < -70 && Math.abs(dx) > Math.abs(dy) * 1.35
    const el = drawerRef.current
    // Smooth release: set the resting transform EXPLICITLY here so
    // the eased transition runs from the user's finger position to
    // the target. The previous version cleared the inline transform
    // before calling onClose — between that clear (which let the
    // open-class transform: 0 take over) and the parent state
    // update, the drawer snapped back to 0 for a frame before
    // animating to -100%. That snap was the visible jitter.
    if (el) {
      el.classList.remove('drawer--dragging')
      if (shouldClose) {
        // Animate from drag position to closed target. Clear the
        // inline transform after the transition completes so the
        // next open doesn't start from translateX(-100%) inline
        // (which would conflict with the .drawer--open class).
        el.style.transform = 'translateX(-100%)'
        const cleanup = () => {
          if (el) el.style.transform = ''
          el.removeEventListener('transitionend', cleanup)
        }
        el.addEventListener('transitionend', cleanup, { once: true })
      } else {
        // Snap-back to open: clearing the inline transform lets
        // the .drawer--open class's translateX(0) take over with
        // the transition running from the drag position.
        el.style.transform = ''
      }
    }
    const suppressGeneratedClick = shouldSuppressDrawerSwipeClick({
      sawHorizontalMove: swipingRef.current,
      dx,
      dy,
    })
    swipingRef.current = false
    panningRef.current = false
    dragStart.current = null
    // A real swipe (drag past the threshold) still emits a synthetic
    // click on the row the finger lifted over. Eat it so the swipe
    // doesn't double as a row selection. A genuine tap never set
    // wasSwiping, so its click passes through untouched.
    if (suppressGeneratedClick) armGeneratedClickSuppression()
    if (shouldClose) onClose?.()
  }
  // touchcancel positions are unreliable across browsers (clientX
  // can be 0 or stale). Treat cancel as "snap back, don't close" —
  // never evaluate the close threshold on a cancel.
  function onTouchCancel() {
    const el = drawerRef.current
    if (el) {
      el.classList.remove('drawer--dragging')
      el.style.transform = ''
    }
    swipingRef.current = false
    panningRef.current = false
    dragStart.current = null
    // touchcancel means the browser owns the gesture (normally native pan-y).
    // It must never leave a click guard behind for a later destination tap.
  }

  // Bind the touch handlers natively so touchmove can be NON-passive (React
  // registers its own touch props passive at the root). A ref carries the latest
  // handler closures so the listeners themselves are attached exactly once for
  // the panel's lifetime — re-attaching on every render would drop a listener
  // mid-gesture, and this must survive a re-render caused by the drag itself.
  const touchHandlersRef = useRef(null)
  touchHandlersRef.current = { onTouchStart, onTouchMove, onTouchEnd, onTouchCancel }
  useEffect(() => {
    const el = drawerRef.current
    if (!el) return undefined
    const start = e => touchHandlersRef.current.onTouchStart(e)
    const move = e => touchHandlersRef.current.onTouchMove(e)
    const end = e => touchHandlersRef.current.onTouchEnd(e)
    const cancel = e => touchHandlersRef.current.onTouchCancel(e)
    el.addEventListener('touchstart', start, { passive: true })
    el.addEventListener('touchmove', move, { passive: false })
    el.addEventListener('touchend', end, { passive: true })
    el.addEventListener('touchcancel', cancel, { passive: true })
    return () => {
      el.removeEventListener('touchstart', start)
      el.removeEventListener('touchmove', move)
      el.removeEventListener('touchend', end)
      el.removeEventListener('touchcancel', cancel)
    }
  }, [])

  function applyResizeWidth(nextWidth) {
    const next = clampDesktopSidebarWidth(nextWidth)
    resizeRef.current.width = next
    drawerRef.current?.closest('.shell')?.style.setProperty(
      '--desktop-sidebar-width',
      `${next}px`,
    )
  }

  function onResizePointerDown(e) {
    if (e.button !== 0) return
    e.preventDefault()
    e.stopPropagation()
    const panelRect = drawerRef.current?.getBoundingClientRect()
    const handleRect = e.currentTarget.getBoundingClientRect()
    const handleCenter = handleRect.left + (handleRect.width / 2)
    const panelCenter = panelRect
      ? panelRect.left + (panelRect.width / 2)
      : handleCenter
    resizeRef.current = {
      pointerId: e.pointerId,
      startX: e.clientX,
      startWidth: clampDesktopSidebarWidth(width),
      edgeDirection: handleCenter < panelCenter ? -1 : 1,
      width: clampDesktopSidebarWidth(width),
    }
    e.currentTarget.setPointerCapture(e.pointerId)
    drawerRef.current?.classList.add('drawer--resizing')
  }

  function onResizePointerMove(e) {
    if (resizeRef.current?.pointerId !== e.pointerId) return
    applyResizeWidth(drawerWidthFromPointerDelta({
      startWidth: resizeRef.current.startWidth,
      startX: resizeRef.current.startX,
      currentX: e.clientX,
      edgeDirection: resizeRef.current.edgeDirection,
    }))
  }

  function finishResize(e) {
    if (resizeRef.current?.pointerId !== e.pointerId) return
    const next = resizeRef.current.width
    resizeRef.current = null
    drawerRef.current?.classList.remove('drawer--resizing')
    if (e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId)
    }
    onWidthChange?.(next)
  }

  function onResizeKeyDown(e) {
    let next = width
    const step = e.shiftKey ? 48 : 16
    if (e.key === 'ArrowLeft') next -= step
    else if (e.key === 'ArrowRight') next += step
    else if (e.key === 'Home') next = DESKTOP_SIDEBAR_MIN_WIDTH
    else if (e.key === 'End') next = DESKTOP_SIDEBAR_MAX_WIDTH
    else return
    e.preventDefault()
    onWidthChange?.(next)
  }

  rowActionInputsRef.current = {
    onChat,
    onApp,
    onDeleteChat,
    onDeleteApp,
    onDeleteAppData,
    setOpenMenu,
    setRenaming,
    setInstallingApp,
    resetAppsSurfaceUi,
    setSharingApp,
    overlayCancelRef,
    renameChat,
    renameApp,
    pinChat,
    pinApp,
    reorderPinned,
  }

  return (
    <>
      {!persistent && (
        <div
          className={`drawer-overlay${open ? ' drawer-overlay--visible' : ''}${scrimBlocking ? ' drawer-overlay--blocking' : ''}`}
          onPointerDown={handleOverlayPointerDown}
        />
      )}
      {/* React 19 reflects the boolean `inert` prop to the boolean
          attribute (present when true, absent when false), so a closed
          drawer is genuinely inert. The old `!open ? '' : undefined` form
          was a no-op: React 19 normalizes the known boolean attribute and
          an empty string serializes as falsy, so the attribute never
          applied and focus/clicks still reached the off-screen drawer. */}
      <nav
        ref={drawerRef}
        id="navigation-drawer"
        className={`drawer${persistent ? ' drawer--persistent' : ''}${interactionLocked ? ' drawer--locked' : ''}${open ? ' drawer--open' : ''}`}
        aria-label="Primary navigation"
        aria-hidden={!open}
        inert={!open}
        tabIndex={-1}
        onPointerDownCapture={clearClickSuppression}
        onKeyDownCapture={clearClickSuppression}
        onClickCapture={onDrawerClickCapture}
        /* Touch gestures are bound natively in the effect above (non-passive
           touchmove), not via React's passive onTouch* props. */
        onTransitionEnd={handleDrawerTransitionEnd}
      >
        {persistent && open && (
          <div
            className="drawer__resize-handle"
            role="separator"
            tabIndex={0}
            aria-label="Resize navigation drawer"
            aria-orientation="vertical"
            aria-valuemin={DESKTOP_SIDEBAR_MIN_WIDTH}
            aria-valuemax={DESKTOP_SIDEBAR_MAX_WIDTH}
            aria-valuenow={clampDesktopSidebarWidth(width)}
            title="Drag to resize navigation"
            onPointerDown={onResizePointerDown}
            onPointerMove={onResizePointerMove}
            onPointerUp={finishResize}
            onPointerCancel={finishResize}
            onLostPointerCapture={finishResize}
            onKeyDown={onResizeKeyDown}
            onDoubleClick={() => onWidthChange?.(DESKTOP_SIDEBAR_DEFAULT_WIDTH)}
          >
            <span aria-hidden="true" />
          </div>
        )}
        <div className="drawer__body">
          <div className="drawer__scroll-wrap">
            <button
              className="drawer__item drawer__item--new"
              onClick={() => {
                resetAppsSurfaceUi({ restoreFocus: false })
                onNewChat()
              }}
            >
              <span className="drawer__item-icon" aria-hidden="true">
                <NewChatNavIcon />
              </span>
              <span className="drawer__item-text">New chat</span>
            </button>

            {searchActive ? (
              <div className="drawer__item drawer__item--search drawer__item--search-open">
                <span className="drawer__item-icon" aria-hidden="true">
                  <SearchNavIcon />
                </span>
                <input
                  ref={searchInputRef}
                  className="drawer__search-input"
                  type="text"
                  inputMode="search"
                  enterKeyHint="search"
                  autoComplete="off"
                  autoCorrect="off"
                  spellCheck="false"
                  value={searchQuery}
                  placeholder="Search chats"
                  aria-label="Search chats"
                  onChange={event => setSearchQuery(event.target.value)}
                  onKeyDown={event => {
                    if (event.key === 'Escape') {
                      // Owns Escape while search is up: first press clears the
                      // drawer back to normal instead of dismissing the drawer.
                      event.stopPropagation()
                      closeSearch()
                    }
                  }}
                />
                <button
                  type="button"
                  className="drawer__search-clear"
                  aria-label="Close search"
                  onClick={closeSearch}
                >
                  <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
                    <path
                      d="M4 4l8 8M12 4l-8 8"
                      stroke="currentColor"
                      strokeWidth="1.6"
                      strokeLinecap="round"
                      fill="none"
                    />
                  </svg>
                </button>
              </div>
            ) : (
              <button
                type="button"
                className="drawer__item drawer__item--search"
                onClick={() => {
                  resetAppsSurfaceUi({ restoreFocus: false })
                  setSearchActive(true)
                }}
              >
                <span className="drawer__item-icon" aria-hidden="true">
                  <SearchNavIcon />
                </span>
                <span className="drawer__item-text">Search chats</span>
              </button>
            )}

            {searchActive && searchTerm ? (
              <div className="drawer__scroll drawer__scroll--navigation">
                <section
                  className="drawer__section"
                  aria-labelledby="drawer-search-label"
                  aria-live="polite"
                >
                  <h2 id="drawer-search-label" className="drawer__label">
                    <span>Results</span>
                  </h2>
                  {searchResults.length > 0 ? searchResults.map(result => (
                    <div className="drawer__row" key={result.id}>
                      <button
                        type="button"
                        className={`drawer__item drawer__item--result${
                          !appsActive && activeView === 'chat' && activeChatId === result.id
                            ? ' drawer__item--active'
                            : ''
                        }`}
                        onClick={() => {
                          // Record the exact transcript row to reveal (when the
                          // hit is a message, not just a title) BEFORE opening
                          // the chat, so ChatView jumps there on first paint.
                          if (result.ts != null && result.role) {
                            requestSearchReveal(
                              result.id,
                              `${result.role}-${result.ts}`,
                              termsFromSnippet(result.snippet),
                            )
                          }
                          rowActions.select('chat', result.id)
                        }}
                      >
                        <span className="drawer__item-text">
                          <span className="drawer__result-title">{result.title}</span>
                          <SearchSnippet text={result.snippet} />
                        </span>
                      </button>
                    </div>
                  )) : searchStatus === 'loading' ? (
                    <p className="drawer__list-status" role="status">Searching…</p>
                  ) : searchStatus === 'error' ? (
                    <p className="drawer__list-status" role="alert">
                      Search is unavailable right now.
                    </p>
                  ) : searchStatus === 'success' ? (
                    <EmptyMessage className="drawer__empty" fill="static">
                      <EmptyMessage.Description>
                        No chats mention “{searchTerm}”
                      </EmptyMessage.Description>
                    </EmptyMessage>
                  ) : null}
                </section>
              </div>
            ) : (
            <div className="drawer__scroll drawer__scroll--navigation" ref={navigationScrollRef}>
              <button
                ref={appsButtonRef}
                type="button"
                className={`drawer__item drawer__item--apps${appsActive ? ' drawer__item--active' : ''}`}
                aria-current={appsActive ? 'page' : undefined}
                onClick={openApps}
              >
                <span className="drawer__item-icon" aria-hidden="true">
                  <AppsNavIcon />
                </span>
                <span className="drawer__item-text">Apps</span>
              </button>

              {pinnedItems.length > 0 && (
                <section className="drawer__section" aria-labelledby="drawer-pinned-label">
                  <h2 id="drawer-pinned-label" className="drawer__label">
                    <span>Pinned</span>
                  </h2>
                  {pinnedItems.map(({ kind, item }) => (
                    <DrawerRow
                      key={`${kind}:${item.id}`}
                      kind={kind}
                      item={item}
                      surface="drawer"
                      streaming={kind === 'chat' && streamingSet.has(item.id)}
                      building={kind === 'app' && !!(item.chat_id && streamingSet.has(item.chat_id))}
                      attention={kind === 'chat'
                        ? attentionSet.has(item.id)
                        : newAppSet.has(Number(item.id))}
                      active={!appsActive && (
                        kind === 'chat'
                          ? activeView === 'chat' && activeChatId === item.id
                          : activeView === 'canvas' && Number(activeAppId) === Number(item.id)
                      )}
                      menuOpen={!!(openMenu
                        && openMenu.surface === 'drawer'
                        && openMenu.kind === kind
                        && openMenu.id === item.id)}
                      menuPlacement={openMenu
                        && openMenu.surface === 'drawer'
                        && openMenu.kind === kind
                        && openMenu.id === item.id
                        ? openMenu.placement
                        : null}
                      renaming={!!(renaming
                        && renaming.surface === 'drawer'
                        && renaming.kind === kind
                        && renaming.id === item.id)}
                      actions={rowActions}
                    />
                  ))}
                </section>
              )}

              <section className="drawer__section" aria-labelledby="drawer-recents-label">
                <h2 id="drawer-recents-label" className="drawer__label drawer__label--recents">
                  <span>Recents</span>
                </h2>
                {allRecents.length > 0 ? visibleRecents.map(({ kind, item }) => (
                  <DrawerRow
                    key={`${kind}:${item.id}`}
                    kind={kind}
                    item={item}
                    surface="drawer"
                    streaming={kind === 'chat' && streamingSet.has(item.id)}
                    building={kind === 'app' && !!(item.chat_id && streamingSet.has(item.chat_id))}
                    attention={kind === 'chat'
                      ? attentionSet.has(item.id)
                      : newAppSet.has(Number(item.id))}
                    active={!appsActive && (
                      kind === 'chat'
                        ? activeView === 'chat' && activeChatId === item.id
                        : activeView === 'canvas' && Number(activeAppId) === Number(item.id)
                    )}
                    menuOpen={!!(openMenu
                      && openMenu.surface === 'drawer'
                      && openMenu.kind === kind
                      && openMenu.id === item.id)}
                    menuPlacement={openMenu
                      && openMenu.surface === 'drawer'
                      && openMenu.kind === kind
                      && openMenu.id === item.id
                      ? openMenu.placement
                      : null}
                    renaming={!!(renaming
                      && renaming.surface === 'drawer'
                      && renaming.kind === kind
                      && renaming.id === item.id)}
                    actions={rowActions}
                  />
                )) : chatsStatus === 'loading' || appsStatus === 'loading' ? (
                  <p className="drawer__list-status" role="status">Loading recents…</p>
                ) : chatsStatus === 'error' || appsStatus === 'error' ? (
                  <div className="drawer__list-status" role="alert">
                    <span>Recents unavailable.</span>
                    <button
                      type="button"
                      onClick={() => {
                        onRetryChats?.()
                        onRetryApps?.()
                      }}
                    >
                      Retry
                    </button>
                  </div>
                ) : (
                  <EmptyMessage className="drawer__empty" fill="static">
                    <EmptyMessage.Description>
                      Nothing recent yet
                    </EmptyMessage.Description>
                  </EmptyMessage>
                )}
                {visibleRecentCount < allRecents.length && (
                  <div
                    ref={recentSentinelRef}
                    className="drawer__progressive-sentinel"
                    aria-hidden="true"
                  />
                )}
              </section>
            </div>
            )}
          </div>{/* /.drawer__scroll-wrap */}

          <div className="drawer__group drawer__group--bottom">
            <button
              className={`drawer__item ${activeView === 'settings' ? 'drawer__item--active' : ''}`}
              aria-label="Settings"
              aria-current={activeView === 'settings' ? 'page' : undefined}
              onClick={() => {
                resetAppsSurfaceUi({ restoreFocus: false })
                onSettings()
              }}
            >
              <span className="drawer__item-icon" aria-hidden="true">
                <SettingsNavIcon />
              </span>
              <span className="drawer__item-text">Settings</span>
              {/* Passive nudge — any provider's refresh token is no
                  longer valid. No banner, no modal: just a quiet dot
                  that says "look here." Settings already owns the
                  reconnect UI. */}
              {settingsWarning && (
                <span
                  className="drawer__settings-warning-dot"
                  aria-label="A provider needs attention"
                  title="A provider needs attention"
                />
              )}
            </button>
          </div>

        </div>
      </nav>
      {appsActive && appsHost && createPortal((
        <AppsDirectory
          empty={sortedApps.length === 0}
          status={appsStatus}
          onRetry={onRetryApps}
          resultCount={filteredApps.length}
          query={appQuery}
          onQueryChange={setAppQuery}
        >
          {filteredApps.map(app => (
            <DrawerRow
              key={app.id}
              kind="app"
              item={app}
              variant="card"
              surface="directory"
              building={!!(app.chat_id && streamingSet.has(app.chat_id))}
              attention={newAppSet.has(Number(app.id))}
              active={activeView === 'canvas' && Number(activeAppId) === Number(app.id)}
              menuOpen={!!(openMenu
                && openMenu.surface === 'directory'
                && openMenu.kind === 'app'
                && openMenu.id === app.id)}
              menuPlacement={openMenu
                && openMenu.surface === 'directory'
                && openMenu.kind === 'app'
                && openMenu.id === app.id
                ? openMenu.placement
                : null}
              renaming={!!(renaming
                && renaming.surface === 'directory'
                && renaming.kind === 'app'
                && renaming.id === app.id)}
              actions={rowActions}
            />
          ))}
        </AppsDirectory>
      ), appsHost)}
      {installingApp && (
        <InstallSheet
          app={installingApp}
          onClose={() => setInstallingApp(null)}
        />
      )}
      {sharingApp && (
        <ShareAppSheet
          app={sharingApp}
          apps={apps}
          onOpenApp={onApp}
          onClose={() => setSharingApp(null)}
        />
      )}
    </>
  )
}


/** One row in the chat or app list — handles select, inline rename,
 * three-dots menu, and confirm-delete in a single self-contained unit
 * so the parent only orchestrates which row is currently expanded. */
const DrawerRow = memo(function DrawerRow({
  kind,
  item,
  variant = 'row',
  surface = 'drawer',
  active,
  streaming,
  // App rows only: the app's owning chat is streaming, i.e. the agent is
  // actively building/editing this app right now. Reuses the streaming
  // dot's animation with a "Building" label so an app under construction
  // pulses the same way an active chat does.
  building,
  attention,
  menuOpen,
  menuPlacement,
  renaming,
  actions,
}) {
  const id = item.id
  const label = kind === 'chat' ? item.title : item.name
  const pinned = !!item.pinned_at
  const slug = item.slug
  const wrapRef = useRef(null)
  const inputRef = useRef(null)
  const holdTimerRef = useRef(null)
  const holdOriginRef = useRef(null)
  const itemButtonRef = useRef(null)
  const suppressCardClickRef = useRef(false)
  const suppressRowClickRef = useRef(false)
  const reorderCleanupRef = useRef(null)
  const secondaryReleaseCleanupRef = useRef(null)
  // Cancel-on-outside-tap during rename. Capture-phase listeners on
  // pointerdown AND click anywhere outside the rename input normally call
  // preventDefault + stopPropagation so another row, Settings, or New chat
  // does NOT fire its own click — the rename just exits. The scrim is the one
  // exception: it remains a reliable drawer-close surface, so a scrim tap
  // cancels the rename and continues to Drawer.handleOverlayPointerDown.
  // Both events are needed: pointerdown prevents focus shift,
  // click is a separate event that some browsers fire regardless.
  // `cancelingRef` tells `commitRename` to bail when the impending
  // blur fires, so the value is discarded rather than committed.
  // `swallowClickRef` tracks that a cancel just happened so the
  // click listener knows to swallow the following click event.
  const cancelingRef = useRef(false)
  const swallowClickRef = useRef(false)
  useEffect(() => {
    if (!renaming) return
    function onOutsidePointer(e) {
      const inputEl = inputRef.current
      if (!inputEl || inputEl.contains(e.target)) return
      cancelingRef.current = true
      actions.cancelRename()
      if (e.target?.closest?.('.drawer-overlay')) return
      e.preventDefault()
      e.stopPropagation()
      swallowClickRef.current = true
    }
    function onOutsideClick(e) {
      if (!swallowClickRef.current) return
      swallowClickRef.current = false
      e.preventDefault()
      e.stopPropagation()
    }
    document.addEventListener('pointerdown', onOutsidePointer, true)
    document.addEventListener('click', onOutsideClick, true)
    return () => {
      document.removeEventListener('pointerdown', onOutsidePointer, true)
      document.removeEventListener('click', onOutsideClick, true)
    }
  }, [renaming, actions])

  useEffect(() => () => {
    if (holdTimerRef.current) clearTimeout(holdTimerRef.current)
    reorderCleanupRef.current?.()
    secondaryReleaseCleanupRef.current?.()
  }, [])

  // Autofocus + select-all on rename open so the user can either retype
  // from scratch or tap into the existing name to edit it.
  useEffect(() => {
    if (renaming && inputRef.current) {
      inputRef.current.focus()
      inputRef.current.select()
    }
  }, [renaming])

  function commitRename() {
    if (cancelingRef.current) {
      cancelingRef.current = false
      return  // outside-tap canceled — discard the value
    }
    const value = inputRef.current?.value || ''
    actions.submitRename(kind, id, label, value.trim())
  }

  function onInputKeyDown(e) {
    if (e.key === 'Enter') { e.preventDefault(); commitRename() }
    else if (e.key === 'Escape') { e.preventDefault(); actions.cancelRename() }
  }

  // Every app card and drawer row enters the same placed action-menu path.
  // Pointer gestures use their real release point; keyboard invocations fall
  // back to the bottom-center of the focused item instead of the event's 0,0.
  function itemMenuPlacement(point) {
    const rect = itemButtonRef.current?.getBoundingClientRect()
    const hasPoint = Number.isFinite(Number(point?.x))
      && Number.isFinite(Number(point?.y))
      && (Number(point.x) !== 0 || Number(point.y) !== 0)
    return {
      clientX: hasPoint ? Number(point.x) : (rect?.left || 0) + (rect?.width || 0) / 2,
      clientY: hasPoint ? Number(point.y) : rect?.bottom || 0,
    }
  }

  function openItemMenu(event) {
    event.preventDefault()
    event.stopPropagation()
    // Chromium raises mouse contextmenu on secondary-button DOWN. The matching
    // UP below owns that gesture and opens only after release, so focus cannot
    // snap back to the source or select a collision-flipped menu item.
    if (event.type === 'contextmenu' && secondaryReleaseCleanupRef.current) return
    actions.toggleMenu(kind, id, true, surface, itemMenuPlacement({
      x: event.clientX,
      y: event.clientY,
    }))
  }

  function beginSecondaryMenuPress(event) {
    if (event.pointerType !== 'mouse' || event.button !== 2) return false
    event.preventDefault()
    secondaryReleaseCleanupRef.current?.()
    const pointerId = event.pointerId
    const placement = itemMenuPlacement({ x: event.clientX, y: event.clientY })
    let timer = null
    const cleanup = () => {
      window.removeEventListener('pointerup', onSecondaryPointerUp, true)
      window.removeEventListener('pointercancel', cleanup, true)
      window.removeEventListener('blur', cleanup, true)
      if (timer !== null) clearTimeout(timer)
      if (secondaryReleaseCleanupRef.current === cleanup) {
        secondaryReleaseCleanupRef.current = null
      }
    }
    const onSecondaryPointerUp = upEvent => {
      if (upEvent.pointerId !== pointerId || upEvent.button !== 2) return
      upEvent.preventDefault()
      cleanup()
      actions.toggleMenu(kind, id, true, surface, placement)
    }
    window.addEventListener('pointerup', onSecondaryPointerUp, true)
    window.addEventListener('pointercancel', cleanup, true)
    window.addEventListener('blur', cleanup, true)
    timer = setTimeout(cleanup, 1500)
    secondaryReleaseCleanupRef.current = cleanup
    return true
  }

  if (renaming) {
    if (variant === 'card') {
      return (
        <div className="apps-directory__card apps-directory__card--editing">
          <input
            ref={inputRef}
            className="drawer__rename-input"
            defaultValue={label}
            onKeyDown={onInputKeyDown}
            onBlur={commitRename}
            aria-label="Rename app"
          />
        </div>
      )
    }
    return (
      <div className={`drawer__item drawer__item--editing ${active ? 'drawer__item--active' : ''}`}>
        <input
          ref={inputRef}
          className="drawer__rename-input"
          defaultValue={label}
          onKeyDown={onInputKeyDown}
          onBlur={commitRename}
          aria-label={`Rename ${kind}`}
        />
      </div>
    )
  }

  if (variant === 'card') {
    function beginCardHold(event) {
      if (event.pointerType === 'mouse' || event.button !== 0) return
      if (holdTimerRef.current) clearTimeout(holdTimerRef.current)
      holdOriginRef.current = { x: event.clientX, y: event.clientY }
      holdTimerRef.current = setTimeout(() => {
        holdTimerRef.current = null
        suppressCardClickRef.current = true
        actions.toggleMenu(
          kind,
          id,
          true,
          surface,
          itemMenuPlacement(holdOriginRef.current),
        )
      }, DRAWER_HOLD_MS)
    }
    function cancelCardHold() {
      if (holdTimerRef.current) clearTimeout(holdTimerRef.current)
      holdTimerRef.current = null
      holdOriginRef.current = null
    }
    return (
      <div className="apps-directory__card">
        <button
          ref={itemButtonRef}
          type="button"
          className="apps-directory__card-main"
          aria-label={`Open ${label}`}
          onClick={() => {
            if (suppressCardClickRef.current) {
              suppressCardClickRef.current = false
              return
            }
            actions.select(kind, id)
          }}
          onContextMenu={openItemMenu}
          onPointerDown={event => {
            if (beginSecondaryMenuPress(event)) return
            beginCardHold(event)
          }}
          onPointerUp={cancelCardHold}
          onPointerCancel={cancelCardHold}
          onPointerMove={event => {
            const origin = holdOriginRef.current
            if (origin && Math.hypot(event.clientX - origin.x, event.clientY - origin.y) > PRE_HOLD_MOVE_PX) {
              cancelCardHold()
            }
          }}
          onKeyDown={event => {
            if (event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')) {
              openItemMenu(event)
            }
          }}
        >
          <AppIcon
            item={item}
            label={label}
            className="apps-directory__card-icon"
            size={128}
          />
          <span className="apps-directory__card-meta">
            <span className="apps-directory__card-name">{label}</span>
          </span>
        </button>
        <DrawerItemMenu
          kind={kind}
          item={item}
          surface={surface}
          pinned={pinned}
          menuOpen={menuOpen}
          actions={actions}
          menuPlacement={menuPlacement}
          restoreFocusRef={itemButtonRef}
        />
      </div>
    )
  }

  function beginPinnedReorder(event) {
    // Any pinned row (chat OR app) reorders on a vertical MOUSE drag — the
    // workspace controller reserves that axis for us and yields it (a rightward
    // pull still lifts the row into a pane). Touch keeps the drawer's own
    // vertical scroll, so reorder stays mouse-only.
    if (!pinned || event.pointerType !== 'mouse' || event.button !== 0) return
    // Finish any still-settling previous drag before measuring, so its lingering
    // transforms can't poison the geometry of this one.
    reorderCleanupRef.current?.()

    const pointerId = event.pointerId
    const start = { x: event.clientX, y: event.clientY }
    const sourceBtn = event.currentTarget
    const drawerEl = sourceBtn.closest('#navigation-drawer')
    const wrapOf = (btn) => btn.closest('.drawer__row') || btn
    // Measure every pinned row once, at drag start.
    const rows = [...(drawerEl || document).querySelectorAll('[data-pinned-key]')]
      .map((btn) => {
        const wrap = wrapOf(btn)
        const rect = wrap.getBoundingClientRect()
        return {
          btn, wrap,
          key: btn.dataset.pinnedKey,
          top: rect.top, height: rect.height, center: rect.top + rect.height / 2,
        }
      })
    const fromIndex = rows.findIndex((r) => r.btn === sourceBtn)
    if (fromIndex < 0) return
    const src = rows[fromIndex]
    let dragging = false
    let listenersOff = false
    let last = { slotDelta: 0, finalKeys: null, changed: false, shifts: new Map() }

    function clearStyles() {
      for (const r of rows) {
        const s = r.wrap.style
        s.transition = ''; s.transform = ''; s.zIndex = ''
        s.position = ''; s.willChange = ''
      }
      src.btn.removeAttribute('data-reordering')
      document.body.style.userSelect = ''
    }
    function removeListeners() {
      if (listenersOff) return
      listenersOff = true
      window.removeEventListener('pointermove', onMove, true)
      window.removeEventListener('pointerup', onUp, true)
      window.removeEventListener('pointercancel', onCancel, true)
    }
    // One-shot teardown for the whole session, guarded so a settle that resolves
    // AFTER a superseding drag has taken over cannot double-commit or wipe the
    // newer drag's styles.
    let ended = false
    function finalize(commit) {
      if (ended) return
      ended = true
      removeListeners()
      if (commit && last.changed && last.finalKeys) actions.reorderPinned(last.finalKeys)
      clearStyles()
      if (reorderCleanupRef.current === forceFinish) reorderCleanupRef.current = null
    }
    // A later drag (or unmount) calls this to snap this one shut with no commit.
    const forceFinish = () => finalize(false)
    reorderCleanupRef.current = forceFinish

    function armDrag() {
      dragging = true
      src.btn.setAttribute('data-reordering', 'true')
      const s = src.wrap.style
      s.zIndex = '6'; s.position = 'relative'; s.transition = 'none'; s.willChange = 'transform'
      for (const r of rows) {
        if (r === src) continue
        r.wrap.style.transition = 'transform 170ms cubic-bezier(0.2, 0, 0, 1)'
        r.wrap.style.willChange = 'transform'
      }
      document.body.style.userSelect = 'none'
    }

    function onMove(moveEvent) {
      if (moveEvent.pointerId !== pointerId) return
      const dx = moveEvent.clientX - start.x
      const dy = moveEvent.clientY - start.y
      if (!dragging) {
        if (Math.abs(dx) > Math.abs(dy) || Math.abs(dy) < 8) return
        armDrag()
      }
      moveEvent.preventDefault()
      last = computePinnedDrag(rows, fromIndex, dy)
      src.wrap.style.transform = `translateY(${dy}px)` // lifted row tracks the pointer 1:1
      for (const r of rows) {
        if (r === src) continue
        r.wrap.style.transform = `translateY(${last.shifts.get(r.key) || 0}px)`
      }
    }

    // Glide the lifted row into the gap the others already opened, then commit
    // and clear together. Because each previewed position equals its final
    // natural position, dropping the transforms as React reorders paints no jump.
    function settle(commit) {
      let animDone = false
      const done = () => {
        if (animDone) return
        animDone = true
        src.wrap.removeEventListener('transitionend', onEnd)
        finalize(commit)
      }
      const onEnd = (ev) => {
        if (ev.target === src.wrap && ev.propertyName === 'transform') done()
      }
      src.wrap.addEventListener('transitionend', onEnd)
      src.wrap.style.transition = 'transform 190ms cubic-bezier(0.2, 0, 0, 1)'
      src.wrap.style.transform = `translateY(${commit ? last.slotDelta : 0}px)`
      // Fallback if transitionend never fires (e.g. the offset was already 0).
      setTimeout(done, 240)
    }

    function onUp(upEvent) {
      if (upEvent.pointerId !== pointerId) return
      removeListeners()
      if (!dragging) { finalize(false); return }
      suppressRowClickRef.current = true
      settle(true)
    }
    function onCancel(cancelEvent) {
      if (cancelEvent.pointerId !== pointerId) return
      removeListeners()
      if (dragging) settle(false)
      else finalize(false)
    }

    window.addEventListener('pointermove', onMove, { capture: true, passive: false })
    window.addEventListener('pointerup', onUp, true)
    window.addEventListener('pointercancel', onCancel, true)
  }

  function onRowPointerDown(event) {
    if (beginSecondaryMenuPress(event)) return
    beginPinnedReorder(event)
  }

  return (
    <div className="drawer__row" ref={wrapRef}>
      <button
        ref={itemButtonRef}
        type="button"
        className={`drawer__item ${active ? 'drawer__item--active' : ''}`}
        aria-current={active ? 'page' : undefined}
        // Drag source for the workspace controller (design §3.1): a delegated
        // pointerdown reads this to lift the row out of the drawer and into a
        // pane. Only present when the splits flag is on; a plain tap still opens
        // in the focused pane (the controller never arms without slop/hold).
        data-drawer-key={`${kind}:${id}`}
        data-drag-key={WORKSPACE_SPLITS_ENABLED ? `${kind}:${id}` : undefined}
        data-pinned-key={pinned ? `${kind}:${id}` : undefined}
        onPointerDown={onRowPointerDown}
        onClick={() => {
          if (suppressRowClickRef.current) {
            suppressRowClickRef.current = false
            return
          }
          actions.select(kind, id)
        }}
        onDoubleClick={event => {
          event.preventDefault()
          actions.startRename(kind, id, surface)
        }}
        onContextMenu={openItemMenu}
        onKeyDown={event => {
          if (event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')) {
            openItemMenu(event)
          }
        }}
      >
        {kind === 'app' && (
          <AppIcon item={item} label={label} className="drawer__app-icon" size={64} />
        )}
        {/* Status dot. Sits before the text so the user's eye
            picks it up alongside the label rather than at the row's
            edge (where the pin lives). aria-label exposes the state. */}
        {streaming ? (
          <span
            className="drawer__streaming-dot"
            aria-label="Currently streaming"
            title="Currently streaming"
          />
        ) : building ? (
          <span
            className="drawer__streaming-dot"
            role="img"
            aria-label="Building"
            title="Building…"
          />
        ) : attention ? (
          <span
            className="drawer__attention-dot"
            role="img"
            aria-label="New activity"
            title="New activity"
          />
        ) : null}
        <span className="drawer__item-text">{label}</span>
      </button>
      <DrawerItemMenu
        kind={kind}
        item={item}
        surface={surface}
        pinned={pinned}
        menuOpen={menuOpen}
        actions={actions}
        menuPlacement={menuPlacement}
        restoreFocusRef={itemButtonRef}
      />
    </div>
  )
})

function AppIcon({ item, label, className, size }) {
  const iconUrl = appIconUrl(item, size)
  const [loadedUrl, setLoadedUrl] = useState(null)
  const hasImage = Boolean(iconUrl && loadedUrl === iconUrl)
  return (
    <span
      className={`${className}${hasImage ? ' is-image' : ''}`}
      style={{ '--app-color': item.background_color || item.theme_color || 'var(--accent)' }}
      aria-hidden="true"
    >
      <span>{appInitials(label)}</span>
      {iconUrl && (
        <img
          src={iconUrl}
          alt=""
          loading="lazy"
          decoding="async"
          onLoad={event => {
            event.currentTarget.hidden = false
            setLoadedUrl(iconUrl)
          }}
          onError={event => {
            event.currentTarget.hidden = true
            setLoadedUrl(null)
          }}
        />
      )}
    </span>
  )
}

function DrawerItemMenu({
  kind,
  item,
  surface,
  pinned,
  menuOpen,
  menuPlacement,
  restoreFocusRef,
  actions,
}) {
  const id = item.id
  const label = kind === 'chat' ? item.title : item.name

  return (
    <DrawerItemActionMenu
      open={menuOpen}
      itemKind={kind}
      itemName={label}
      icon={kind === 'app'
        ? (
          <AppIcon
            item={item}
            label={label}
            className="drawer__item-action-icon"
            size={64}
          />
        )
        : null}
      pinned={pinned}
      canInstall={kind === 'app' && Boolean(item.slug)}
      canShare={kind === 'app' && isDrawerAppShareEligible(item)}
      placement={menuPlacement}
      restoreFocusRef={restoreFocusRef}
      onClose={() => actions.toggleMenu(kind, id, false, surface)}
      onPin={() => actions.pin(kind, id, !pinned)}
      onRename={() => actions.startRename(kind, id, surface)}
      onInstall={() => actions.install(item)}
      onShare={() => actions.share(item)}
      onDelete={() => actions.remove(kind, id)}
      onDeleteData={() => actions.removeData(id)}
    />
  )
}
