import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useQueryClient } from '@tanstack/react-query'
import { EmptyMessage } from '@openai/apps-sdk-ui/components/EmptyMessage'
import { Pause, Play, Stop } from '@openai/apps-sdk-ui/components/Icon'
import { api } from '../../api/client.js'
import { appQueries, chatQueries } from '../../hooks/queries.js'
import { useHistoryDismiss } from '../../hooks/useHistoryDismiss.jsx'
import {
  AppsNavIcon,
  NewChatNavIcon,
  SettingsNavIcon,
} from '../navigationIcons.js'
import AppIcon from '../AppIcon.jsx'
import { preloadAppIcons } from '../appIcon.js'
import {
  computePinnedDrag,
  observePinnedOrderHandoff,
  pinnedEntriesMatchRanks,
  pinnedOrderHandoffStatus,
  projectPinnedEntries,
} from './pinnedReorder.js'
import {
  drawerCloseWatchdogMs,
  drawerWidthFromPointerDelta,
  isGeneratedTouchClick,
  isHorizontalDrawerSwipe,
  shouldRestoreDrawerFocus,
  shouldSuppressDrawerSwipeClick,
  shouldAutoRevealActiveChat,
  clearDrawerGestureStyles,
} from '../../lib/drawerLifecycle.js'
import {
  PRESS_MENU_HOLD_MS,
  PRE_HOLD_MOVE_PX,
} from '../Shell/dragController.js'
import InstallSheet from './InstallSheet.jsx'
import AppsDirectory from './AppsDirectory.jsx'
import DrawerItemActionMenu from './DrawerItemActionMenu.jsx'
import {
  buildDrawerSections,
  filterInstalledApps,
  findDrawerMenuItem,
} from './drawerInformationArchitecture.js'
import ShareAppSheet from './ShareAppSheet.jsx'
import { isDrawerAppShareEligible } from './appShareState.js'
import {
  clampDrawerRowWindow,
  drawerRowSpacerHeights,
  drawerRowWindow,
  drawerRowWindowForIndex,
  initialDrawerRowWindow,
  sameDrawerRowWindow,
} from './drawerRowWindow.js'
import {
  clampDesktopSidebarWidth,
  DESKTOP_SIDEBAR_DEFAULT_WIDTH,
  DESKTOP_SIDEBAR_MAX_WIDTH,
  DESKTOP_SIDEBAR_MIN_WIDTH,
} from '../Shell/useDesktopSidebar.js'
import { captureLayoutSpace, clientLengthToLayout } from '../../lib/layoutSpace.js'
import './Drawer.css'

// Module-level constant so default Set props are stable across renders.
// A fresh `new Set()` per call would break identity-based memoization
// downstream.
const EMPTY_SET = new Set()
const TOUCH_CONTEXT_MENU_PROVENANCE_MS = 1500
const APP_ICON_PRIORITY_COUNT = 24
const APP_ICON_WARM_LIMIT = 96

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
  nowPlaying,
  onNowPlayingOpen,
  onNowPlayingControl,
  // Set of chat ids whose agent is currently streaming. Used to
  // show a small accent dot next to the row label so the user can
  // see at a glance which background builds are still running.
  // Sourced from Shell (the only place that knows when a turn is
  // active across the whole app). Defaults to an empty Set so the
  // drawer renders cleanly if no parent supplies the prop.
  streamingChatIds,
  // Set of chat ids with an interaction waiting on the owner. This state takes
  // visual precedence over streaming because the agent cannot make progress
  // until the owner acts.
  ownerInputChatIds,
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
  // Shared flag raised while a row is being dragged into the workspace. The
  // swipe-to-close handlers stand down for that same pointer stream.
  dragActiveRef,
  // The workspace controller owns the row pointer until its held intent is
  // clear, then calls the matching row for menu/reorder work.
  drawerRowGesturesRef,
}) {
  const streamingSet = streamingChatIds || EMPTY_SET
  const ownerInputSet = ownerInputChatIds || EMPTY_SET
  const attentionSet = attentionChatIds || EMPTY_SET
  const newAppSet = newAppIds || EMPTY_SET
  // One source of truth for which row the focused pane is showing, so a chat
  // and an app are selected by the same rule wherever the row is rendered.
  const isRowActive = ({ kind, item }) => (
    kind === 'chat'
      ? activeView === 'chat' && activeChatId === item.id
      : activeView === 'canvas' && Number(activeAppId) === Number(item.id)
  )
  const resizeRef = useRef(null)
  const pinnedReorderGenerationRef = useRef(0)
  const [pinnedOrderHandoff, setPinnedOrderHandoff] = useState(null)
  const {
    pinned: basePinnedItems,
    recents: allRecents,
    apps: sortedApps,
  } = useMemo(() => buildDrawerSections(chats, apps), [chats, apps])

  // Decode the first launcher viewport immediately, then warm a bounded
  // remainder without competing with initial shell work.
  useEffect(() => {
    if (sortedApps.length === 0) return
    const priority = sortedApps.slice(0, APP_ICON_PRIORITY_COUNT)
    const remainder = sortedApps.slice(APP_ICON_PRIORITY_COUNT, APP_ICON_WARM_LIMIT)
    void preloadAppIcons(priority)
    if (remainder.length === 0 || navigator.connection?.saveData) return

    const warmRemainder = () => { void preloadAppIcons(remainder) }
    if (typeof requestIdleCallback === 'function') {
      requestIdleCallback(warmRemainder, { timeout: 3000 })
    } else {
      setTimeout(warmRemainder, 500)
    }
  }, [sortedApps])
  const pinnedItems = useMemo(() => (
    pinnedOrderHandoff
      ? projectPinnedEntries(basePinnedItems, pinnedOrderHandoff.visibleKeys)
      : basePinnedItems
  ), [basePinnedItems, pinnedOrderHandoff])

  // Keep the chosen order through every partial chat/app refresh. Release only
  // when both query observers carry the exact server ranks returned by the
  // atomic save; useLayoutEffect makes the authority handoff before paint.
  useLayoutEffect(() => {
    if (!pinnedOrderHandoff) return
    const currentKeys = basePinnedItems.map(({ kind, item }) => `${kind}:${item.id}`)
    const status = pinnedOrderHandoffStatus(
      currentKeys,
      pinnedOrderHandoff.visibleKeys,
    )
    if (status === 'superseded') {
      setPinnedOrderHandoff(null)
      return
    }
    if (
      pinnedOrderHandoff.releaseRanks
      && pinnedEntriesMatchRanks(basePinnedItems, pinnedOrderHandoff.releaseRanks)
    ) setPinnedOrderHandoff(null)
  }, [basePinnedItems, pinnedOrderHandoff])
  const [recentWindow, setRecentWindow] = useState(
    () => initialDrawerRowWindow(allRecents.length),
  )
  const navigationScrollRef = useRef(null)
  const recentSectionRef = useRef(null)
  const recentRowsStartRef = useRef(null)
  const recentSectionTopRef = useRef(0)
  const recentWindowRafRef = useRef(0)
  const revealedActiveChatRef = useRef(null)
  const visibleRecents = useMemo(
    () => allRecents.slice(recentWindow.start, recentWindow.end),
    [allRecents, recentWindow],
  )
  const recentSpacers = drawerRowSpacerHeights(recentWindow, allRecents.length)

  // Keep one viewport-sized DOM window no matter how far the owner scrolls.
  // Top/bottom spacers preserve the exact numeric scroll position, so unlike
  // resetting progressive rows on close this does not reintroduce the phone
  // drawer jump. Clamp only when deletion makes the list shorter.
  useEffect(() => {
    setRecentWindow(current => {
      const next = clampDrawerRowWindow(current, allRecents.length)
      return sameDrawerRowWindow(current, next) ? current : next
    })
  }, [allRecents.length])

  const measureRecentWindow = useCallback(() => {
    const root = navigationScrollRef.current
    const section = recentSectionRef.current
    const rowsStart = recentRowsStartRef.current
    if (!root || !section || !rowsStart) return
    const rootRect = root.getBoundingClientRect()
    const rowsRect = rowsStart.getBoundingClientRect()
    const rectDelta = clientLengthToLayout(
      rowsRect.top - rootRect.top,
      captureLayoutSpace(root),
    )
    recentSectionTopRef.current = rectDelta + root.scrollTop
    const next = drawerRowWindow({
      total: allRecents.length,
      scrollTop: root.scrollTop,
      viewportHeight: root.clientHeight,
      sectionTop: recentSectionTopRef.current,
    })
    setRecentWindow(current => (
      sameDrawerRowWindow(current, next) ? current : next
    ))
  }, [allRecents.length])

  // Measure once before an opened drawer paints, then update the small row
  // window at most once per animation frame while it scrolls. The scroll path
  // reads only scrollTop/clientHeight; section geometry is refreshed on open or
  // when the pinned/Recent boundary changes.
  useLayoutEffect(() => {
    if (!open) return
    measureRecentWindow()
  }, [measureRecentWindow, open, pinnedItems.length])
  useEffect(() => {
    if (!open) return undefined
    const root = navigationScrollRef.current
    if (!root) return undefined
    const onScroll = () => {
      if (recentWindowRafRef.current) return
      recentWindowRafRef.current = requestAnimationFrame(() => {
        recentWindowRafRef.current = 0
        const next = drawerRowWindow({
          total: allRecents.length,
          scrollTop: root.scrollTop,
          viewportHeight: root.clientHeight,
          sectionTop: recentSectionTopRef.current,
        })
        setRecentWindow(current => (
          sameDrawerRowWindow(current, next) ? current : next
        ))
      })
    }
    root.addEventListener('scroll', onScroll, { passive: true })
    return () => {
      root.removeEventListener('scroll', onScroll)
      cancelAnimationFrame(recentWindowRafRef.current)
      recentWindowRafRef.current = 0
    }
  }, [allRecents.length, open])

  // The always-visible desktop sidebar follows chat selections made elsewhere.
  // The phone drawer instead preserves its last manual scroll position: opening
  // navigation must not double as an automatic list scroll. Older desktop rows
  // may sit outside the mounted window, so first move that bounded window to
  // the target row, then reveal it before paint. Remember the completed
  // reveal so recency refreshes cannot fight a later manual scroll.
  useLayoutEffect(() => {
    if (!shouldAutoRevealActiveChat({ open, persistent, activeView, activeChatId })) {
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

    const revealWindow = drawerRowWindowForIndex(
      recentWindow,
      allRecents.length,
      recentIndex,
    )
    if (revealWindow !== recentWindow) {
      setRecentWindow(revealWindow)
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
    persistent,
    pinnedItems,
    recentWindow,
  ])

  // The drawer owns one action menu. Rows provide item identity, placement,
  // and a focus-return target without mounting their own controllers.
  const [openMenu, setOpenMenu] = useState(null)
  const menuRestoreFocusRef = useRef(null)
  const activeMenuItem = findDrawerMenuItem(openMenu, chats, apps)
  const {
    open: openItemMenuHistory,
    close: closeItemMenu,
  } = useHistoryDismiss(() => setOpenMenu(null))

  function showItemMenu({ restoreFocusTarget, ...menu }) {
    openItemMenuHistory()
    menuRestoreFocusRef.current = restoreFocusTarget
    setOpenMenu(menu)
  }
  // A deleted row cannot keep owning the shared menu.
  useEffect(() => {
    if (openMenu && !activeMenuItem) closeItemMenu()
  }, [openMenu, activeMenuItem, closeItemMenu])
  const [renamingState, setRenamingState] = useState(null) // { kind, id } | null
  // Mirrors `renaming` synchronously (not via useEffect — that's one render
  // behind) so outside-tap cancellation sees the current edit immediately.
  const renamingRef = useRef(null)
  const overlayCancelRef = useRef(false)
  // Closing on pointerdown makes the drawer feel immediate, but WebKit may
  // retarget the compatibility click from that same touch to content revealed
  // beneath the scrim. Keep ownership of exactly that click. A new pointer or
  // keyboard activation clears a stale claim first, so a touch sequence that
  // produces no click cannot poison the owner's next action.
  const outsideDismissClickRef = useRef(false)
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
    closeItemMenu()
    setRenaming(null)
    if (restoreFocus) {
      requestAnimationFrame(() => appsButtonRef.current?.focus())
    }
  }, [closeItemMenu, setRenaming])

  function openApps() {
    setAppQuery('')
    closeItemMenu()
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
    openMenu(menu) {
      rowActionInputsRef.current.showItemMenu(menu)
    },
    closeMenu() {
      rowActionInputsRef.current.closeItemMenu()
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
    outsideDismissClickRef.current = true
    e.preventDefault()
    e.stopPropagation()
    onClose?.()
  }

  useEffect(() => {
    function releaseStaleDismissClaim() {
      outsideDismissClickRef.current = false
    }
    function consumeOutsideDismissClick(e) {
      if (!outsideDismissClickRef.current) return
      outsideDismissClickRef.current = false
      e.preventDefault()
      e.stopPropagation()
    }
    // Capture sees a new activation before the overlay's bubble handler can
    // claim its own pointerdown. The click listener then catches both an
    // ordinary scrim click and WebKit's retargeted compatibility click.
    document.addEventListener('pointerdown', releaseStaleDismissClaim, true)
    document.addEventListener('keydown', releaseStaleDismissClaim, true)
    document.addEventListener('click', consumeOutsideDismissClick, true)
    return () => {
      document.removeEventListener('pointerdown', releaseStaleDismissClaim, true)
      document.removeEventListener('keydown', releaseStaleDismissClaim, true)
      document.removeEventListener('click', consumeOutsideDismissClick, true)
    }
  }, [])

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

  async function publishHostedApp(id) {
    const res = await api.apps.publishHosted(id)
    if (!res.ok) {
      let message = 'Could not update public access.'
      try {
        const payload = await res.json()
        if (typeof payload?.detail === 'string') message = payload.detail
      } catch {}
      throw new Error(message)
    }
    const updated = await res.json()
    setSharingApp(updated)
    refreshApps()
    return updated
  }

  async function stopHostedApp(id) {
    const res = await api.apps.stopHosted(id)
    if (!res.ok) {
      let message = 'Could not stop public access.'
      try {
        const payload = await res.json()
        if (typeof payload?.detail === 'string') message = payload.detail
      } catch {}
      throw new Error(message)
    }
    const updated = await res.json()
    setSharingApp(updated)
    refreshApps()
    return updated
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
  // it). The visible handoff remains authoritative until the ONE atomic server
  // transaction returns exact ranks for both query caches. This prevents an
  // unrelated chat/app refresh from exposing a mixed snapshot mid-save.
  async function reorderPinned(orderedKeys) {
    if (!Array.isArray(orderedKeys) || orderedKeys.length === 0) return
    const generation = ++pinnedReorderGenerationRef.current
    const visibleKeys = [...orderedKeys]
    const items = visibleKeys.map(key => {
      const sep = key.indexOf(':')
      return { kind: key.slice(0, sep), id: key.slice(sep + 1) }
    })
    if (items.some(item => !['chat', 'app'].includes(item.kind) || !item.id)) return

    setPinnedOrderHandoff({ generation, visibleKeys, releaseRanks: null })
    const chatKey = chatQueries.keys.all
    const appKey = appQueries.keys.all
    try {
      const res = await api.chats.reorderPinned(items)
      if (!res.ok) throw new Error('Could not reorder pinned items')
      const payload = await res.json()
      const persisted = Array.isArray(payload?.items) ? payload.items : []
      const rank = new Map(persisted.map(item => [
        `${item.kind}:${item.id}`,
        item.pinned_at,
      ]))
      if (rank.size !== visibleKeys.length || visibleKeys.some(key => !rank.has(key))) {
        throw new Error('Pinned reorder returned an incomplete order')
      }
      if (generation !== pinnedReorderGenerationRef.current) return
      // Cancel reads started before the transaction committed; a stale response
      // must not overwrite the coherent ranks below.
      await Promise.all([
        queryClient.cancelQueries({ queryKey: chatKey }),
        queryClient.cancelQueries({ queryKey: appKey }),
      ])
      if (generation !== pinnedReorderGenerationRef.current) return
      const applyRank = kind => list => (list || []).map(item => {
        const pinnedAt = rank.get(`${kind}:${item.id}`)
        return pinnedAt ? { ...item, pinned_at: pinnedAt } : item
      })
      queryClient.setQueryData(chatKey, applyRank('chat'))
      queryClient.setQueryData(appKey, applyRank('app'))
      setPinnedOrderHandoff(current => (
        current?.generation === generation
          ? {
            ...current,
            releaseRanks: visibleKeys.map(key => ({ key, pinnedAt: rank.get(key) })),
          }
          : current
      ))
    } catch {
      if (generation === pinnedReorderGenerationRef.current) {
        setPinnedOrderHandoff(null)
      }
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
  const closeShieldRef = useRef(null)
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
      // back on the toggle that opened it (or whatever was focused). Do not
      // steal focus back from a destination that already accepted the handoff.
      const shouldRestore = shouldRestoreDrawerFocus({
        drawer: drawerRef.current,
        activeElement: document.activeElement,
        body: document.body,
      })
      if (shouldRestore
          && previousFocusRef.current
          && typeof previousFocusRef.current.focus === 'function') {
        previousFocusRef.current.focus()
      }
      previousFocusRef.current = null
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

  // Swipe-left-to-close. Pointer Events and the panel's
  // `touch-action: pan-y pinch-zoom` divide ownership up front: the browser
  // keeps vertical drawer scrolling on its native path, while this component
  // receives the horizontal pointer stream. pointerdown captures origin,
  // pointermove drags the panel 1:1 with the finger, and pointerup either
  // closes (≥70px past origin AND horizontal-
  // dominant) or snaps back. The CSS transition is disabled mid-
  // drag via `drawer--dragging` so the panel tracks the finger
  // without easing.
  // `touch-action` is the standards-level scroll arbitration primitive. Keeping
  // gesture ownership declarative means the browser never has to wait for a
  // cancelable JavaScript touchmove before beginning an ordinary drawer scroll.
  // The gesture record keeps its sticky horizontal/panning classifications
  // together across renders. Horizontal movement may need to suppress one
  // compatibility click; native vertical scrolling never does.
  const drawerGestureRef = useRef(null)
  const suppressGeneratedClickRef = useRef(false)

  function clearClickSuppression() {
    suppressGeneratedClickRef.current = false
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
    drawerGestureRef.current = null
  }, [open, persistent])

  // Keep only the panel's still-visible footprint hit-testable while it slides
  // away. The full-screen visual scrim stops owning input as soon as close is
  // acknowledged; a geometry-matched shield follows the panel until its
  // transition ends. This keeps uncovered controls live without allowing taps
  // through visible drawer pixels.
  const [scrimBlocking, setScrimBlocking] = useState(open && !persistent)
  useLayoutEffect(() => {
    if (open && !persistent) {
      closeShieldRef.current?.style.setProperty('--drawer-close-start-x', '0px')
      setScrimBlocking(true)
    }
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
    const width = e.currentTarget.offsetWidth
    const x = new DOMMatrixReadOnly(getComputedStyle(e.currentTarget).transform).m41
    if (x > -width + 1) return
    setScrimBlocking(false)
  }

  function handleCloseShieldAnimationEnd(e) {
    if (open || e.target !== e.currentTarget || e.animationName !== 'drawer-close-shield') return
    setScrimBlocking(false)
  }

  function onDrawerPointerDown(e) {
    if (e.pointerType !== 'touch' || !e.isPrimary) return
    if (!open || persistent || interactionLocked) return
    // A row is being lifted out of the drawer — the drag controller owns this
    // gesture; swipe-to-close stands down (design §3.1).
    if (dragActiveRef?.current) { drawerGestureRef.current = null; return }
    drawerGestureRef.current = {
      x: e.clientX,
      y: e.clientY,
      pointerId: e.pointerId,
      horizontal: false,
      panning: false,
      layoutSpace: captureLayoutSpace(drawerRef.current),
      width: drawerRef.current.offsetWidth,
    }
  }
  function onDrawerPointerMove(e) {
    // Stand down mid-gesture too: a hold that armed the controller after
    // touchstart must not also pan the panel. The controller owns its own
    // pointer stream; releasing here without cancelling leaves the gesture to it.
    const gesture = drawerGestureRef.current
    if (dragActiveRef?.current) {
      if (gesture) gesture.panning = false
      return
    }
    if (!gesture || e.pointerId !== gesture.pointerId) return
    const dx = e.clientX - gesture.x
    const dy = e.clientY - gesture.y
    const isHorizontalSwipe = isHorizontalDrawerSwipe(dx, dy)
    // Only custom horizontal gestures need the one-shot click suppressor.
    // Native vertical scrolling already owns its tap/click cancellation; arming
    // our suppressor for it made a quick post-scroll destination tap look dead.
    if (isHorizontalSwipe) {
      gesture.horizontal = true
    }
    if (dx < 0 && isHorizontalSwipe) gesture.panning = true
    if (!gesture.panning) return
    const el = drawerRef.current
    if (!el) return
    try {
      if (!el.hasPointerCapture?.(e.pointerId)) el.setPointerCapture?.(e.pointerId)
    } catch { /* capture is optional; touch-action still owns arbitration */ }
    el.classList.add('drawer--dragging')
    // Keep the panel between fully closed and fully open. The close threshold
    // below intentionally remains a physical finger-travel distance.
    const layoutX = Math.min(0, Math.max(
      clientLengthToLayout(dx, gesture.layoutSpace),
      -gesture.width,
    ))
    el.style.transform = `translateX(${layoutX}px)`
    closeShieldRef.current?.style.setProperty('--drawer-close-start-x', `${layoutX}px`)
  }
  function onDrawerPointerUp(e) {
    // If the controller took over, it owns pointerup too — do nothing here.
    if (dragActiveRef?.current) { drawerGestureRef.current = null; return }
    const gesture = drawerGestureRef.current
    if (!gesture || e.pointerId !== gesture.pointerId) return
    const dx = e.clientX - gesture.x
    const dy = e.clientY - gesture.y
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
        // Animate from the drag position to the closed target. The open-state
        // layout effect clears this inline value as soon as the parent commits
        // the closed class, whose transform has the same target.
        el.style.transform = 'translateX(-100%)'
      } else {
        // Snap-back to open: clearing the inline transform lets
        // the .drawer--open class's translateX(0) take over with
        // the transition running from the drag position.
        el.style.transform = ''
        closeShieldRef.current?.style.setProperty('--drawer-close-start-x', '0px')
      }
    }
    const suppressGeneratedClick = shouldSuppressDrawerSwipeClick({
      sawHorizontalMove: gesture.horizontal,
      dx,
      dy,
    })
    drawerGestureRef.current = null
    // A real swipe (drag past the threshold) still emits a synthetic
    // click on the row the finger lifted over. Eat it so the swipe
    // doesn't double as a row selection. A genuine tap never set
    // wasSwiping, so its click passes through untouched.
    if (suppressGeneratedClick) suppressGeneratedClickRef.current = true
    if (shouldClose) onClose?.()
  }
  // pointercancel positions are unreliable across browsers (clientX
  // can be 0 or stale). Treat cancel as "snap back, don't close" —
  // never evaluate the close threshold on a cancel.
  function onDrawerPointerCancel(e) {
    const gesture = drawerGestureRef.current
    if (gesture && e.pointerId !== gesture.pointerId) return
    clearDrawerGestureStyles(drawerRef.current)
    closeShieldRef.current?.style.setProperty('--drawer-close-start-x', '0px')
    drawerGestureRef.current = null
    // pointercancel means the browser owns the gesture (normally native pan-y).
    // It must never leave a click guard behind for a later destination tap.
  }

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
    const panel = drawerRef.current
    const panelRect = panel?.getBoundingClientRect()
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
      layoutSpace: captureLayoutSpace(panel || document.documentElement),
    }
    e.currentTarget.setPointerCapture(e.pointerId)
    drawerRef.current?.classList.add('drawer--resizing')
  }

  function onResizePointerMove(e) {
    if (resizeRef.current?.pointerId !== e.pointerId) return
    const layoutDelta = clientLengthToLayout(
      e.clientX - resizeRef.current.startX,
      resizeRef.current.layoutSpace,
    )
    applyResizeWidth(drawerWidthFromPointerDelta({
      startWidth: resizeRef.current.startWidth,
      delta: layoutDelta,
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
    showItemMenu,
    closeItemMenu,
    menuRestoreFocusRef,
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
        <>
          <div
            className={`drawer-overlay${open ? ' drawer-overlay--visible' : ''}${scrimBlocking ? ' drawer-overlay--blocking' : ''}`}
            onPointerDown={handleOverlayPointerDown}
          />
          <div
            ref={closeShieldRef}
            className={`drawer-close-shield${!open && scrimBlocking ? ' drawer-close-shield--active' : ''}`}
            aria-hidden="true"
            onAnimationEnd={handleCloseShieldAnimationEnd}
          />
        </>
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
        onPointerDown={onDrawerPointerDown}
        onPointerMove={onDrawerPointerMove}
        onPointerUp={onDrawerPointerUp}
        onPointerCancel={onDrawerPointerCancel}
        onKeyDownCapture={clearClickSuppression}
        onClickCapture={onDrawerClickCapture}
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

            <div className="drawer__scroll drawer__scroll--navigation" ref={navigationScrollRef}>
              <button
                ref={appsButtonRef}
                type="button"
                className={`drawer__item drawer__item--apps${activeView === 'apps' ? ' drawer__item--active' : ''}`}
                aria-current={activeView === 'apps' ? 'page' : undefined}
                onClick={openApps}
              >
                <span className="drawer__item-icon" aria-hidden="true">
                  <AppsNavIcon />
                </span>
                <span className="drawer__item-text">Apps</span>
              </button>

              {nowPlaying && (
                <NowPlaying
                  session={nowPlaying}
                  app={apps.find(app => String(app.id) === String(nowPlaying.appId))}
                  onOpen={onNowPlayingOpen}
                  onControl={onNowPlayingControl}
                />
              )}

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
                      needsOwnerInput={kind === 'chat'
                        ? ownerInputSet.has(item.id)
                        : !!(item.chat_id && ownerInputSet.has(item.chat_id))}
                      streaming={kind === 'chat' && streamingSet.has(item.id)}
                      building={kind === 'app' && !!(item.chat_id && streamingSet.has(item.chat_id))}
                      attention={kind === 'chat'
                        ? attentionSet.has(item.id)
                        : newAppSet.has(Number(item.id))}
                      active={isRowActive({ kind, item })}
                      renaming={!!(renaming
                        && renaming.surface === 'drawer'
                        && renaming.kind === kind
                        && renaming.id === item.id)}
                      actions={rowActions}
                      dragActiveRef={dragActiveRef}
                      drawerRowGesturesRef={drawerRowGesturesRef}
                    />
                  ))}
                </section>
              )}

              <section
                ref={recentSectionRef}
                className="drawer__section"
                aria-labelledby="drawer-recents-label"
              >
                <h2 id="drawer-recents-label" className="drawer__label drawer__label--recents">
                  <span>Recents</span>
                </h2>
                <div ref={recentRowsStartRef} aria-hidden="true" />
                {recentSpacers.before > 0 && (
                  <div
                    className="drawer__virtual-spacer"
                    style={{ height: recentSpacers.before }}
                    aria-hidden="true"
                  />
                )}
                {allRecents.length > 0 ? visibleRecents.map(({ kind, item }) => (
                  <DrawerRow
                    key={`${kind}:${item.id}`}
                    kind={kind}
                    item={item}
                    surface="drawer"
                    needsOwnerInput={kind === 'chat'
                      ? ownerInputSet.has(item.id)
                      : !!(item.chat_id && ownerInputSet.has(item.chat_id))}
                    streaming={kind === 'chat' && streamingSet.has(item.id)}
                    building={kind === 'app' && !!(item.chat_id && streamingSet.has(item.chat_id))}
                    attention={kind === 'chat'
                      ? attentionSet.has(item.id)
                      : newAppSet.has(Number(item.id))}
                    active={isRowActive({ kind, item })}
                    renaming={!!(renaming
                      && renaming.surface === 'drawer'
                      && renaming.kind === kind
                      && renaming.id === item.id)}
                    actions={rowActions}
                    dragActiveRef={dragActiveRef}
                    drawerRowGesturesRef={drawerRowGesturesRef}
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
                {recentSpacers.after > 0 && (
                  <div
                    className="drawer__virtual-spacer"
                    style={{ height: recentSpacers.after }}
                    aria-hidden="true"
                  />
                )}
              </section>
            </div>
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
              needsOwnerInput={!!(app.chat_id && ownerInputSet.has(app.chat_id))}
              building={!!(app.chat_id && streamingSet.has(app.chat_id))}
              attention={newAppSet.has(Number(app.id))}
              active={activeView === 'canvas' && Number(activeAppId) === Number(app.id)}
              renaming={!!(renaming
                && renaming.surface === 'directory'
                && renaming.kind === 'app'
                && renaming.id === app.id)}
              actions={rowActions}
            />
          ))}
        </AppsDirectory>
      ), appsHost)}
      <DrawerItemMenu
        menu={openMenu}
        item={activeMenuItem}
        actions={rowActions}
        restoreFocusRef={menuRestoreFocusRef}
      />
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
          onPublish={publishHostedApp}
          onStop={stopHostedApp}
          onClose={() => setSharingApp(null)}
        />
      )}
    </>
  )
}

function NowPlaying({ session, app, onOpen, onControl }) {
  const appName = app?.name || 'App'
  const paused = session.playbackState === 'paused'
  const loading = session.playbackState === 'loading'
  const stateLabel = loading ? 'Preparing' : paused ? 'Paused' : 'Playing'
  return (
    <section className="drawer__now-playing" aria-label={`Now playing from ${appName}`}>
      <button
        type="button"
        className="drawer__now-playing-main"
        onClick={() => onOpen?.(session.appId)}
        aria-label={`Open ${appName}`}
      >
        <AppIcon item={app} label={appName} className="drawer__now-playing-icon" />
        <span className="drawer__now-playing-copy">
          <strong>{session.title}</strong>
          <small>{appName} · {stateLabel}</small>
        </span>
      </button>
      <button
        type="button"
        className="drawer__now-playing-control"
        aria-label={paused ? 'Resume playback' : 'Pause playback'}
        title={paused ? 'Resume' : 'Pause'}
        disabled={loading}
        onClick={() => onControl?.(paused ? 'play' : 'pause')}
      >
        {paused ? <Play aria-hidden="true" /> : <Pause aria-hidden="true" />}
      </button>
      <button
        type="button"
        className="drawer__now-playing-control"
        aria-label="Stop playback"
        title="Stop"
        onClick={() => onControl?.('stop')}
      >
        <Stop aria-hidden="true" />
      </button>
    </section>
  )
}


/** One row in the chat or app list — handles select, inline rename,
 * contextual actions, and confirm-delete in a single self-contained unit
 * so the parent only orchestrates which row is currently expanded. */
const DrawerRow = memo(function DrawerRow({
  kind,
  item,
  variant = 'row',
  surface = 'drawer',
  active,
  needsOwnerInput,
  streaming,
  // App rows only: the app's owning chat is streaming, i.e. the agent is
  // actively building/editing this app right now. Reuses the streaming
  // dot's animation with a "Building" label so an app under construction
  // pulses the same way an active chat does.
  building,
  attention,
  renaming,
  actions,
  dragActiveRef,
  drawerRowGesturesRef,
}) {
  const id = item.id
  const label = kind === 'chat' ? item.title : item.name
  const pinned = !!item.pinned_at
  const waiting = kind === 'chat' && !!item.waiting
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
  const drawerGestureHandlerRef = useRef(null)
  // iOS may dispatch its long-press contextmenu AFTER pointercancel. Keep the
  // touch provenance independent of the live pointer session so that delayed
  // native event cannot open actions before a release. It expires, and keyboard
  // or mouse input clears it, so accessibility and desktop context menus remain.
  const touchMenuPointerAtRef = useRef(0)
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

  drawerGestureHandlerRef.current = {
    openMenu: point => openItemMenuAt(point, itemButtonRef.current),
    beginReorder: session => beginPinnedReorder(session),
  }
  useLayoutEffect(() => {
    if (variant === 'card' || !drawerRowGesturesRef?.current) return undefined
    const key = `${kind}:${id}`
    const registry = drawerRowGesturesRef.current
    registry.set(key, drawerGestureHandlerRef)
    return () => {
      if (registry.get(key) === drawerGestureHandlerRef) {
        registry.delete(key)
      }
    }
  }, [drawerRowGesturesRef, id, kind, variant])

  // Autofocus + select-all on rename open so the user can either retype from
  // scratch or tap into the existing name to edit it. Defer one frame: opening
  // rename tears down the action menu, and focusing synchronously here races
  // the browser moving focus off the just-unmounted menu item, which overrides
  // the editor focus. A frame later the teardown has settled and focus sticks.
  useEffect(() => {
    if (!renaming) return undefined
    const frame = requestAnimationFrame(() => {
      const input = inputRef.current
      if (!input) return
      input.focus()
      input.select()
    })
    return () => cancelAnimationFrame(frame)
  }, [renaming])

  function commitRename() {
    if (cancelingRef.current) {
      cancelingRef.current = false
      return  // outside-tap canceled — discard the value
    }
    const value = inputRef.current?.value || ''
    actions.submitRename(kind, id, label, value.trim())
  }

  function onRenameBlur() {
    const input = inputRef.current
    requestAnimationFrame(() => {
      const active = document.activeElement
      if (active === input) return
      // Closing the action menu calls history.back() to release its Back-stack
      // entry; that popstate drops focus to <body> a frame later. That is the
      // app moving focus, not the user leaving the field, so reclaim it instead
      // of committing — otherwise the just-opened editor blur-commits and
      // vanishes. A real exit tabs to another control; outside-pointer and
      // Escape cancel through their own paths (cancelingRef).
      const focusDropped = !active
        || active === document.body
        || active === document.documentElement
      if (focusDropped && input?.isConnected && !cancelingRef.current) {
        input.focus()
        return
      }
      commitRename()
    })
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

  function openItemMenuAt(
    point,
    trigger = itemButtonRef.current,
    { focusFirstAction = false } = {},
  ) {
    actions.openMenu({
      kind,
      id,
      surface,
      placement: itemMenuPlacement(point),
      focusFirstAction,
      restoreFocusTarget: trigger,
    })
  }

  function suppressTouchContextMenu(event) {
    // On Android, contextmenu is itself a PointerEvent and may arrive before
    // pointerup or pointercancel. Stop it during capture so React's bubble-phase
    // menu opener cannot mistake the browser's hold threshold for our release.
    // Provenance remains as a fallback for browsers that expose it as a plain
    // MouseEvent; keyboard input clears that provenance below.
    const contextPointerType = event.nativeEvent?.pointerType || ''
    const freshTouchPointer = touchMenuPointerAtRef.current > 0
      && performance.now() - touchMenuPointerAtRef.current < TOUCH_CONTEXT_MENU_PROVENANCE_MS
    const fromTouch = contextPointerType === 'touch' || contextPointerType === 'pen'
    if (event.type !== 'contextmenu' || (!fromTouch && !freshTouchPointer)) return false
    event.preventDefault()
    event.stopPropagation()
    event.nativeEvent?.stopImmediatePropagation?.()
    return true
  }

  function openItemMenu(event) {
    // Touch menus open only from the shared gesture controller during its
    // stationary hold. Native contextmenu would steal that timing and re-open
    // actions on release, so suppress it; mouse and keyboard remain semantic.
    if (suppressTouchContextMenu(event)) return
    event.preventDefault()
    event.stopPropagation()
    // Chromium raises mouse contextmenu on secondary-button DOWN. The matching
    // UP below owns that gesture and opens only after release, so focus cannot
    // snap back to the source or select a collision-flipped menu item.
    if (event.type === 'contextmenu' && secondaryReleaseCleanupRef.current) return
    openItemMenuAt({
      x: event.clientX,
      y: event.clientY,
    }, event.currentTarget, {
      focusFirstAction: event.type === 'keydown',
    })
  }

  function recordItemMenuPointer(event) {
    touchMenuPointerAtRef.current = event.pointerType === 'mouse'
      ? 0
      : performance.now()
  }

  function beginSecondaryMenuPress(event) {
    if (event.pointerType !== 'mouse' || event.button !== 2) return false
    event.preventDefault()
    secondaryReleaseCleanupRef.current?.()
    const sourceBtn = event.currentTarget
    const pointerId = event.pointerId
    const openingPoint = { x: event.clientX, y: event.clientY }
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
      openItemMenuAt(openingPoint, sourceBtn)
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
            onBlur={onRenameBlur}
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
          onBlur={onRenameBlur}
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
        openItemMenuAt(holdOriginRef.current)
      }, PRESS_MENU_HOLD_MS)
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
          onContextMenuCapture={suppressTouchContextMenu}
          onContextMenu={openItemMenu}
          onPointerDown={event => {
            recordItemMenuPointer(event)
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
            touchMenuPointerAtRef.current = 0
            if (event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')) {
              openItemMenu(event)
            }
          }}
        >
          <AppIcon
            item={item}
            label={label}
            className="apps-directory__card-icon"
          />
          <span className="apps-directory__card-meta">
            <span className="apps-directory__card-name">{label}</span>
          </span>
        </button>
      </div>
    )
  }

  function beginPinnedReorder({ pointerId, start, moveEvent }) {
    if (!pinned || !moveEvent) return false
    // Finish any still-settling previous drag before this gesture can measure;
    // lingering transforms from the old preview would poison the new geometry.
    reorderCleanupRef.current?.()

    const sourceBtn = itemButtonRef.current
    if (!sourceBtn) return false
    const previousBodySelect = document.body.style.userSelect
    let dragging = false
    let listenersOff = false
    let cancelOrderHandoff = null
    let stylesCleared = false
    let rows = []
    let fromIndex = -1
    let src = null
    let pinnedSection = null
    let ownsDragClaim = false
    let last = { slotDelta: 0, finalKeys: null, changed: false, shifts: new Map() }

    function releaseDragClaim() {
      if (!ownsDragClaim) return
      ownsDragClaim = false
      dragActiveRef.current = false
    }

    function clearStyles() {
      if (stylesCleared) return
      stylesCleared = true
      cancelOrderHandoff?.()
      for (const r of rows) {
        const s = r.wrap.style
        s.transition = ''; s.transform = ''; s.zIndex = ''
        s.position = ''; s.willChange = ''
      }
      src?.btn.removeAttribute('data-reordering')
      document.body.style.userSelect = previousBodySelect
      releaseDragClaim()
      try {
        if (sourceBtn.hasPointerCapture?.(pointerId)) sourceBtn.releasePointerCapture(pointerId)
      } catch { /* capture may already be gone at pointerup */ }
      if (reorderCleanupRef.current === forceFinish) reorderCleanupRef.current = null
    }
    function removeListeners() {
      if (listenersOff) return
      listenersOff = true
      window.removeEventListener('pointermove', onMove, true)
      window.removeEventListener('pointerup', onUp, true)
      window.removeEventListener('pointercancel', onCancel, true)
      window.removeEventListener('blur', forceFinish, true)
      window.removeEventListener('pagehide', forceFinish, true)
      window.removeEventListener('contextmenu', suppressNativeContextMenu, true)
      document.removeEventListener('visibilitychange', onVisibility, true)
    }
    // One-shot teardown for the whole session, guarded so a settle that resolves
    // AFTER a superseding drag has taken over cannot double-commit or wipe the
    // newer drag's styles.
    let ended = false
    let transformSpace = null
    function finalize(commit) {
      if (ended) return
      ended = true
      removeListeners()
      if (commit && last.changed && last.finalKeys && pinnedSection) {
        // Keep preview pixels authoritative until React moves the keyed rows.
        // MutationObserver runs after that DOM move and before paint.
        cancelOrderHandoff = observePinnedOrderHandoff(
          pinnedSection,
          last.finalKeys,
          clearStyles,
        )
        actions.reorderPinned(last.finalKeys)
        return
      }
      clearStyles()
    }
    // A later drag (or unmount) calls this to snap this one shut with no commit.
    const forceFinish = () => {
      if (!ended) {
        ended = true
        removeListeners()
      }
      clearStyles()
    }
    function onVisibility() {
      if (document.visibilityState === 'hidden') forceFinish()
    }
    function suppressNativeContextMenu(contextEvent) {
      contextEvent.preventDefault()
    }
    reorderCleanupRef.current = forceFinish

    function measureRows() {
      const drawerEl = sourceBtn.closest('#navigation-drawer')
      transformSpace = captureLayoutSpace(drawerEl || document.documentElement)
      pinnedSection = sourceBtn.closest('.drawer__section')
      const wrapOf = (btn) => btn.closest('.drawer__row') || btn
      // Measure once only after the held row resolves to vertical reordering.
      rows = [...(drawerEl || document).querySelectorAll('[data-pinned-key]')]
        .map((btn) => {
          const wrap = wrapOf(btn)
          const rect = wrap.getBoundingClientRect()
          const top = clientLengthToLayout(
            rect.top - transformSpace.clientTop,
            transformSpace,
          )
          const height = clientLengthToLayout(rect.height, transformSpace)
          return {
            btn, wrap,
            key: btn.dataset.pinnedKey,
            top, height, center: top + height / 2,
          }
        })
      fromIndex = rows.findIndex((r) => r.btn === sourceBtn)
      src = rows[fromIndex]
      return fromIndex >= 0
    }
    function armDrag() {
      if (!measureRows()) return false
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
      return true
    }

    function onMove(moveEvent) {
      if (moveEvent.pointerId !== pointerId) return
      const dy = clientLengthToLayout(
        moveEvent.clientY - start.y,
        transformSpace,
      )
      moveEvent.preventDefault()
      last = computePinnedDrag(rows, fromIndex, dy)
      src.wrap.style.transform = `translateY(${dy}px)`
      for (const r of rows) {
        if (r === src) continue
        const shift = last.shifts.get(r.key) || 0
        r.wrap.style.transform = `translateY(${shift}px)`
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
      const settleY = commit ? last.slotDelta : 0
      src.wrap.style.transform = `translateY(${settleY}px)`
      // Fallback if transitionend never fires (e.g. the offset was already 0).
      setTimeout(done, 240)
    }

    function onUp(upEvent) {
      if (upEvent.pointerId !== pointerId) return
      removeListeners()
      releaseDragClaim()
      if (!dragging) { finalize(false); return }
      suppressRowClickRef.current = true
      settle(true)
    }
    function onCancel(cancelEvent) {
      if (cancelEvent.pointerId !== pointerId) return
      removeListeners()
      releaseDragClaim()
      if (dragging) {
        settle(false)
        return
      }
      finalize(false)
    }

    window.addEventListener('pointermove', onMove, { capture: true, passive: false })
    window.addEventListener('pointerup', onUp, true)
    window.addEventListener('pointercancel', onCancel, true)
    window.addEventListener('blur', forceFinish, true)
    window.addEventListener('pagehide', forceFinish, true)
    window.addEventListener('contextmenu', suppressNativeContextMenu, true)
    document.addEventListener('visibilitychange', onVisibility, true)
    if (!armDrag()) {
      finalize(false)
      return false
    }
    ownsDragClaim = true
    dragActiveRef.current = true
    try { sourceBtn.setPointerCapture?.(pointerId) } catch { /* capture optional */ }
    onMove(moveEvent)
    return true
  }

  function onRowPointerDown(event) {
    // A compatibility click follows the completed gesture without a new
    // pointerdown. If no such click arrived, this is a genuinely new gesture
    // and must retire the old one-shot guard rather than inherit it.
    suppressRowClickRef.current = false
    recordItemMenuPointer(event)
    beginSecondaryMenuPress(event)
  }

  return (
    <div className={`drawer__row${active ? ' drawer__row--active' : ''}`} ref={wrapRef}>
      <button
        ref={itemButtonRef}
        type="button"
        className={`drawer__item ${active ? 'drawer__item--active' : ''}`}
        aria-current={active ? 'page' : undefined}
        // One shared controller resolves a held row only after intent is clear:
        // staying still opens actions, vertical movement reorders a pin, and
        // outward movement places it in the workspace.
        data-drawer-key={`${kind}:${id}`}
        data-drag-key={`${kind}:${id}`}
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
          suppressRowClickRef.current = false
          touchMenuPointerAtRef.current = 0
          if (event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')) {
            openItemMenu(event)
          }
        }}
      >
        {kind === 'app' && (
          <AppIcon item={item} label={label} className="drawer__app-icon" />
        )}
        {/* Status dot. Sits before the text so the user's eye
            picks it up alongside the label rather than at the row's
            edge (where the pin lives). aria-label exposes the state. */}
        {needsOwnerInput ? (
          <span
            className="drawer__owner-input-dot"
            role="img"
            aria-label="Your input is needed"
            title="Your input is needed"
          />
        ) : streaming ? (
          <span
            className="drawer__streaming-dot"
            aria-label="Currently streaming"
            title="Currently streaming"
          />
        ) : waiting ? (
          <span
            className="drawer__waiting-icon"
            role="img"
            aria-label="Waiting to resume"
            title="Waiting to resume"
          >
            <Pause width={8} height={8} aria-hidden="true" />
          </span>
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
    </div>
  )
})

const DrawerItemMenu = memo(function DrawerItemMenu({
  menu,
  item,
  actions,
  restoreFocusRef,
}) {
  const kind = menu?.kind || 'chat'
  const id = menu?.id
  const surface = menu?.surface || 'drawer'
  const pinned = !!item?.pinned_at
  const label = item ? (kind === 'chat' ? item.title : item.name) : ''

  return (
    <DrawerItemActionMenu
      open={Boolean(menu && item)}
      itemKind={kind}
      itemName={label}
      pinned={pinned}
      canInstall={kind === 'app' && Boolean(item?.slug)}
      canShare={kind === 'app' && isDrawerAppShareEligible(item)}
      placement={menu?.placement}
      focusFirstAction={menu?.focusFirstAction === true}
      restoreFocusRef={restoreFocusRef}
      onClose={actions.closeMenu}
      onPin={() => actions.pin(kind, id, !pinned)}
      onRename={() => actions.startRename(kind, id, surface)}
      onInstall={() => actions.install(item)}
      onShare={() => actions.share(item)}
      onDelete={() => actions.remove(kind, id)}
      onDeleteData={() => actions.removeData(id)}
    />
  )
})
