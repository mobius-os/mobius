import { useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Plus, Chats, Grid, DotsVerticalMoreMenu, SettingsCog, Pin, PinFilled } from '@openai/apps-sdk-ui/components/Icon'
import { Menu } from '@openai/apps-sdk-ui/components/Menu'
import { EmptyMessage } from '@openai/apps-sdk-ui/components/EmptyMessage'
import { apiFetch } from '../../api/client.js'
import { appQueries, chatQueries } from '../../hooks/queries.js'
import InstallSheet from './InstallSheet.jsx'
import './Drawer.css'

// Module-level constant so default Set props are stable across renders.
// A fresh `new Set()` per call would break identity-based memoization
// downstream.
const EMPTY_SET = new Set()

export default function Drawer({
  open,
  onClose,
  apps,
  activeView,
  activeAppId,
  chats,
  activeChatId,
  onChat,
  onApp,
  // Open a chat/app as a pinned tab in the shell tab strip: onOpenInTab(kind, id).
  onOpenInTab,
  onNewChat,
  onDeleteChat,
  onDeleteApp,
  onDeleteAppData,
  onSettings,
  // Set of chat ids whose agent is currently streaming. Used to
  // pulse a small accent dot next to the row label so the user can
  // see at a glance which background builds are still running.
  // Sourced from Shell (the only place that knows when a turn is
  // active across the whole app). Defaults to an empty Set so the
  // drawer renders cleanly if no parent supplies the prop.
  streamingChatIds,
  // Set of chat ids whose latest background run finished while the
  // user was elsewhere. Rendered as a steady attention dot, distinct
  // from the animated streaming dot above.
  attentionChatIds,
  // Set of app ids that first appeared in the fetched list this session
  // (freshly built or App-Store-installed). Rendered as the same steady
  // accent dot as chat attention, cleared by Shell when the app is opened —
  // an arrival cue for an app that otherwise lands silently at the bottom
  // of the oldest-first list.
  newAppIds,
  // Truthy when any registered provider's refresh token is no longer
  // valid. Drives a small warning dot on the Settings row — passive
  // nudge toward Reconnect, no modal, no banner.
  settingsWarning,
}) {
  const streamingSet = streamingChatIds || EMPTY_SET
  const attentionSet = attentionChatIds || EMPTY_SET
  const newAppSet = newAppIds || EMPTY_SET
  // Pinned-first sort: pinned rows by pinned_at desc, then unpinned by
  // activity_at desc (owner-send), with updated_at as a fallback. Server
  // returns this order already (see routes/chats.py list_chats), but we
  // mirror it defensively so the drawer stays correct if the cache holds
  // an older response.
  const allChats = (chats || [])
    .filter(c => c.has_messages)
    .sort((a, b) => {
      const ap = a.pinned_at, bp = b.pinned_at
      if (ap && !bp) return -1
      if (!ap && bp) return 1
      if (ap && bp) return bp.localeCompare(ap)
      return ((b.activity_at || b.updated_at) || '')
        .localeCompare((a.activity_at || a.updated_at) || '')
    })
  // Mirror the same sort for apps. Server delivers it, but the cache
  // may carry a stale list. Unpinned apps stay in creation order so
  // the existing "stable apps list" UX (oldest-first within unpinned)
  // is preserved.
  const sortedApps = (apps || []).slice().sort((a, b) => {
    const ap = a.pinned_at, bp = b.pinned_at
    if (ap && !bp) return -1
    if (!ap && bp) return 1
    if (ap && bp) return bp.localeCompare(ap)
    return (a.created_at || '').localeCompare(b.created_at || '')
  })

  // One row at a time can be in rename or open-menu mode. Tracking the
  // active id (rather than per-row state) lets a click on another row's
  // ⋮ button close any open menu without needing a global outside-click
  // handler per row.
  const [openMenu, setOpenMenu] = useState(null) // { kind, id } | null
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
  // The app whose "Add to home screen" sheet is open ({id,name,slug}),
  // or null. Mirrors openMenu/renamingState — one at a time, owned here
  // rather than in Shell so this stays drawer-local.
  const [installingApp, setInstallingApp] = useState(null)

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

  // Mirrors `renaming` synchronously (not via useEffect — that's
  // one render behind). The overlay's pointerdown handler must see
  // the latest value the same task the user starts renaming, so we
  // wrap the state setter and update the ref inline.
  // `overlayCancelRef` is set by the overlay's pointerdown when a
  // rename is active; read by the rename-submit callback to skip
  // the PATCH (overlay-tap = cancel, not commit) and by the
  // overlay's click to suppress the drawer-close.
  const renamingRef = useRef(null)
  const overlayCancelRef = useRef(false)
  const renaming = renamingState
  const setRenaming = (next) => {
    renamingRef.current = next
    setRenamingState(next)
  }

  function handleOverlayPointerDown() {
    if (renamingRef.current) overlayCancelRef.current = true
  }
  function handleOverlayClick() {
    if (renamingRef.current || overlayCancelRef.current) {
      // Overlay was tapped during a rename. The blur has already
      // fired → onRenameSubmit will see overlayCancelRef and skip
      // the PATCH. Here we just suppress the drawer-close.
      renamingRef.current = null
      overlayCancelRef.current = false
      setRenaming(null)
      return
    }
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
    const res = await apiFetch(`/chats/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ title }),
    })
    if (res.ok) refreshChats()
  }

  async function renameApp(id, name) {
    const res = await apiFetch(`/apps/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ name }),
    })
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
    const res = await apiFetch(`/chats/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ pinned }),
    })
    if (res.ok) refreshChats()
    else queryClient.setQueryData(key, prev)
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
    const res = await apiFetch(`/apps/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ pinned }),
    })
    if (res.ok) refreshApps()
    else queryClient.setQueryData(key, prev)
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
  useEffect(() => {
    if (open) {
      previousFocusRef.current = document.activeElement
      // Defer to next frame so the drawer's CSS transition has begun
      // and the panel is in the rendered DOM before we focus it.
      requestAnimationFrame(() => {
        drawerRef.current?.focus()
      })
    } else {
      // Restore focus when the drawer closes so keyboard users land
      // back on the toggle that opened it (or whatever was focused).
      if (previousFocusRef.current && typeof previousFocusRef.current.focus === 'function') {
        previousFocusRef.current.focus()
        previousFocusRef.current = null
      }
    }
  }, [open])

  // Escape key closes the drawer while it is open.
  useEffect(() => {
    if (!open) return
    function onKeyDown(e) {
      if (e.key === 'Escape') {
        e.stopPropagation()
        onClose?.()
      }
    }
    document.addEventListener('keydown', onKeyDown, { capture: true })
    return () => document.removeEventListener('keydown', onKeyDown, { capture: true })
  }, [open, onClose])

  // Swipe-left-to-close. Mirror of the mobius-design-iter pattern:
  // touchstart captures origin, touchmove drags the panel 1:1 with
  // the finger when the gesture is dominantly horizontal-left,
  // touchend either closes (≥70px past origin AND horizontal-
  // dominant) or snaps back. The CSS transition is disabled mid-
  // drag via `drawer--dragging` so the panel tracks the finger
  // without easing.
  const drawerRef = useRef(null)
  const dragStart = useRef(null) // { x, y } or null
  // True once a touch gesture has moved past the tap/swipe threshold.
  // React's onTouch* handlers are passive, so preventDefault() in
  // onTouchMove is a no-op and a horizontal drag still emits a synthetic
  // click that lands on whatever row the finger lifted over — selecting
  // a chat/app the user only meant to swipe past. We can't cancel the
  // touch, but we CAN swallow the click it produces: on touchend after a
  // real swipe, install a one-shot capture-phase click listener on the
  // <nav> that eats the very next click, then removes itself. A genuine
  // tap never crosses the threshold, so its click passes through and the
  // row still selects.
  const swipingRef = useRef(false)
  // Movement (px) beyond which a gesture is a swipe, not a tap.
  const SWIPE_THRESHOLD = 10

  function onTouchStart(e) {
    if (!open || e.touches.length !== 1) return
    swipingRef.current = false
    dragStart.current = { x: e.touches[0].clientX, y: e.touches[0].clientY }
  }
  function onTouchMove(e) {
    if (!dragStart.current || e.touches.length !== 1) return
    const dx = e.touches[0].clientX - dragStart.current.x
    const dy = e.touches[0].clientY - dragStart.current.y
    // Any movement past the threshold (in either axis) means this is a
    // drag, not a tap — mark it so touchend can suppress the trailing
    // click even on a scroll/vertical pan that lifted over a row.
    if (Math.abs(dx) > SWIPE_THRESHOLD || Math.abs(dy) > SWIPE_THRESHOLD) {
      swipingRef.current = true
    }
    if (dx < 0 && Math.abs(dx) > Math.abs(dy) * 1.15) {
      const el = drawerRef.current
      if (!el) return
      el.classList.add('drawer--dragging')
      el.style.transform = `translateX(${Math.max(dx, -320)}px)`
    }
  }
  function onTouchEnd(e) {
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
    const wasSwiping = swipingRef.current
    swipingRef.current = false
    dragStart.current = null
    // A real swipe (drag past the threshold) still emits a synthetic
    // click on the row the finger lifted over. Eat it so the swipe
    // doesn't double as a row selection. A genuine tap never set
    // wasSwiping, so its click passes through untouched.
    if (wasSwiping) suppressNextClick()
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
    const wasSwiping = swipingRef.current
    swipingRef.current = false
    dragStart.current = null
    // Mirror touchend: if the cancelled gesture had already become a
    // swipe, the browser may still deliver a click — suppress it too.
    if (wasSwiping) suppressNextClick()
  }

  // Install a one-shot capture-phase click listener on the drawer <nav>
  // that swallows the very next click, then removes itself. This is the
  // reliable way to neutralize the synthetic click a touch-drag emits:
  // React's touch handlers are passive, so preventDefault() in
  // onTouchMove can't stop the gesture from producing a click. Capturing
  // on the <nav> intercepts the click before it reaches any row handler.
  // We also arm a short fallback timeout to drop the listener if no click
  // ever arrives (e.g. the lift was outside any clickable target), so a
  // later legitimate tap is never eaten by a stale suppressor.
  function suppressNextClick() {
    const el = drawerRef.current
    if (!el) return
    let cleared = false
    const onClickCapture = (ev) => {
      ev.stopPropagation()
      ev.preventDefault()
      clear()
    }
    function clear() {
      if (cleared) return
      cleared = true
      el.removeEventListener('click', onClickCapture, true)
      clearTimeout(timer)
    }
    el.addEventListener('click', onClickCapture, true)
    const timer = setTimeout(clear, 400)
  }

  return (
    <>
      <div
        className={`drawer-overlay ${open ? 'drawer-overlay--visible' : ''}`}
        onPointerDown={handleOverlayPointerDown}
        onClick={handleOverlayClick}
      />
      {/* React 19 reflects the boolean `inert` prop to the boolean
          attribute (present when true, absent when false), so a closed
          drawer is genuinely inert. The old `!open ? '' : undefined` form
          was a no-op: React 19 normalizes the known boolean attribute and
          an empty string serializes as falsy, so the attribute never
          applied and focus/clicks still reached the off-screen drawer. */}
      <nav
        ref={drawerRef}
        id="navigation-drawer"
        className={`drawer ${open ? 'drawer--open' : ''}`}
        aria-hidden={!open}
        inert={!open}
        tabIndex={-1}
        onTouchStart={onTouchStart}
        onTouchMove={onTouchMove}
        onTouchEnd={onTouchEnd}
        onTouchCancel={onTouchCancel}
      >
        <div className="drawer__body">

          {/* Single scroll wrapper around New chat + Chats + Apps.
              Earlier each group held its own scrolling region with
              flex:1, which split the drawer height evenly even when
              one section had a few rows and the other had many. With
              one outer scroll, sections size to content; if total
              content overflows, the whole column scrolls and the
              bottom edge fades via mask-image so a half-row at the
              boundary doesn't read as abruptly cut off. Settings
              sits outside this wrapper at the drawer bottom. */}
          <div className="drawer__scroll-wrap">

            <button className="drawer__item drawer__item--new" onClick={onNewChat}>
              <span className="drawer__item-icon" aria-hidden="true">
                <Plus width={18} height={18} />
              </span>
              <span className="drawer__item-text">New chat</span>
            </button>

          <div className="drawer__group drawer__group--chats">
            <h2 className="drawer__label drawer__label--chats">
              <Chats width={16} height={16} aria-hidden="true" />
              <span>Chats</span>
            </h2>
            <div className="drawer__scroll">
              {allChats.length > 0 ? allChats.map(chat => (
                <DrawerRow
                  key={chat.id}
                  kind="chat"
                  id={chat.id}
                  label={chat.title}
                  pinned={!!chat.pinned_at}
                  streaming={streamingSet.has(chat.id)}
                  attention={attentionSet.has(chat.id)}
                  active={activeView === 'chat' && activeChatId === chat.id}
                  onSelect={() => onChat(chat.id)}
                  menuOpen={!!(openMenu && openMenu.kind === 'chat' && openMenu.id === chat.id)}
                  onMenuToggle={(next) => setOpenMenu(next ? { kind: 'chat', id: chat.id } : null)}
                  renaming={renaming && renaming.kind === 'chat' && renaming.id === chat.id}
                  onRenameStart={() => setRenaming({ kind: 'chat', id: chat.id })}
                  onRenameCancel={() => setRenaming(null)}
                  onRenameSubmit={(next) => {
                    setRenaming(null)
                    if (overlayCancelRef.current) {
                      overlayCancelRef.current = false
                      return  // overlay-tap cancel — discard the value
                    }
                    if (next && next !== chat.title) renameChat(chat.id, next)
                  }}
                  onPin={(next) => pinChat(chat.id, next)}
                  onOpenInTab={onOpenInTab ? () => onOpenInTab('chat', chat.id) : undefined}
                  onDelete={() => onDeleteChat(chat.id)}
                />
              )) : (
                <EmptyMessage className="drawer__empty" fill="static">
                  <EmptyMessage.Description>
                    No conversations yet
                  </EmptyMessage.Description>
                </EmptyMessage>
              )}
            </div>
          </div>

          {apps.length > 0 && (
            <div className="drawer__group drawer__group--apps">
              <h2 className="drawer__label drawer__label--apps">
                <Grid width={16} height={16} aria-hidden="true" />
                <span>Apps</span>
              </h2>
              <div className="drawer__scroll">
                {sortedApps.map(app => (
                  <DrawerRow
                    key={app.id}
                    kind="app"
                    id={app.id}
                    label={app.name}
                    slug={app.slug}
                    pinned={!!app.pinned_at}
                    building={!!(app.chat_id && streamingSet.has(app.chat_id))}
                    attention={newAppSet.has(Number(app.id))}
                    active={activeView === 'canvas' && Number(activeAppId) === Number(app.id)}
                    onSelect={() => onApp(app.id)}
                    menuOpen={!!(openMenu && openMenu.kind === 'app' && openMenu.id === app.id)}
                    onMenuToggle={(next) => setOpenMenu(next ? { kind: 'app', id: app.id } : null)}
                    renaming={renaming && renaming.kind === 'app' && renaming.id === app.id}
                    onRenameStart={() => setRenaming({ kind: 'app', id: app.id })}
                    onRenameCancel={() => setRenaming(null)}
                    onRenameSubmit={(next) => {
                      setRenaming(null)
                      if (overlayCancelRef.current) {
                        overlayCancelRef.current = false
                        return  // overlay-tap cancel — discard the value
                      }
                      if (next && next !== app.name) renameApp(app.id, next)
                    }}
                    onPin={(next) => pinApp(app.id, next)}
                    onOpenInTab={onOpenInTab ? () => onOpenInTab('app', app.id) : undefined}
                    onDelete={() => onDeleteApp?.(app.id)}
                    onDeleteData={() => onDeleteAppData?.(app.id)}
                    onInstall={() => setInstallingApp({ id: app.id, name: app.name, slug: app.slug, updatedAt: app.updated_at })}
                  />
                ))}
              </div>
            </div>
          )}

          </div>{/* /.drawer__scroll-wrap */}

          <div className="drawer__group drawer__group--bottom">
            <button
              className={`drawer__item ${activeView === 'settings' ? 'drawer__item--active' : ''}`}
              onClick={onSettings}
            >
              <SettingsCog width={16} height={16} aria-hidden="true" style={{ flexShrink: 0 }} />
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
      {installingApp && (
        <InstallSheet
          appId={installingApp.id}
          appName={installingApp.name}
          appSlug={installingApp.slug}
          appUpdatedAt={installingApp.updatedAt}
          onClose={() => setInstallingApp(null)}
        />
      )}
    </>
  )
}


/** One row in the chat or app list — handles select, inline rename,
 * three-dots menu, and confirm-delete in a single self-contained unit
 * so the parent only orchestrates which row is currently expanded. */
function DrawerRow({
  kind,
  label,
  pinned,
  active,
  slug,
  streaming,
  // App rows only: the app's owning chat is streaming, i.e. the agent is
  // actively building/editing this app right now. Reuses the streaming
  // dot's animation with a "Building" label so an app under construction
  // pulses the same way an active chat does.
  building,
  attention,
  onSelect,
  menuOpen,
  onMenuToggle,
  renaming,
  onRenameStart,
  onRenameCancel,
  onRenameSubmit,
  onPin,
  onOpenInTab,
  onDelete,
  onDeleteData,
  onInstall,
}) {
  const wrapRef = useRef(null)
  const inputRef = useRef(null)
  const [confirmingDelete, setConfirmingDelete] = useState(false)
  // Separate two-step confirm for the app-only "Delete data" action, which
  // wipes stored data but keeps the app installed. Independent of
  // confirmingDelete so the two confirm chips can't collide.
  const [confirmingDeleteData, setConfirmingDeleteData] = useState(false)

  // Reset the inline-confirm two-steps (apps only) whenever the menu
  // closes — otherwise reopening would land the user back on the
  // primed "Confirm delete?" view. The SDK Menu (Radix) handles
  // open/close, outside-click, escape, and collision-aware
  // positioning natively; we just listen for the close.
  useEffect(() => {
    if (!menuOpen) {
      setConfirmingDelete(false)
      setConfirmingDeleteData(false)
    }
  }, [menuOpen])

  // Cancel-on-outside-tap during rename. Capture-phase listeners on
  // pointerdown AND click anywhere outside the rename input call
  // preventDefault + stopPropagation so the tapped element (overlay,
  // another row, Settings, New chat) does NOT fire its own click —
  // the rename just exits without selecting anything else.
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
      e.preventDefault()
      e.stopPropagation()
      cancelingRef.current = true
      swallowClickRef.current = true
      onRenameCancel()
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
  }, [renaming, onRenameCancel])

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
    onRenameSubmit(value.trim())
  }

  function onInputKeyDown(e) {
    if (e.key === 'Enter') { e.preventDefault(); commitRename() }
    else if (e.key === 'Escape') { e.preventDefault(); onRenameCancel() }
  }

  if (renaming) {
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

  return (
    <div className="drawer__row" ref={wrapRef}>
      <button
        type="button"
        className={`drawer__item ${active ? 'drawer__item--active' : ''}`}
        onClick={onSelect}
      >
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
            aria-label="Building"
            title="Building…"
          />
        ) : attention ? (
          <span
            className="drawer__attention-dot"
            aria-label="New activity"
            title="New activity"
          />
        ) : null}
        <span className="drawer__item-text">{label}</span>
        {pinned && (
          <span className="drawer__item-pin" aria-label="Pinned" title="Pinned">
            <PinFilled width={14} height={14} />
          </span>
        )}
      </button>
      <Menu
        forceOpen={menuOpen}
        onOpen={() => onMenuToggle(true)}
        onClose={() => onMenuToggle(false)}
      >
        <Menu.Trigger>
          {/* No Tooltip wrap here. Both Menu.Trigger and Tooltip
              are Radix asChild wrappers that merge their props
              onto the first child element. Nesting them breaks
              the click-prop chain — Menu's onClick lands on the
              Tooltip wrapper instead of the button, so tapping
              ⋮ did nothing. The aria-label below covers screen
              readers; visible tooltip discovery is a nice-to-have
              we can revisit later with a different composition. */}
          <button
            type="button"
            className="drawer__more"
            aria-label={`More actions for ${label}`}
          >
            <DotsVerticalMoreMenu width={16} height={16} aria-hidden="true" />
          </button>
        </Menu.Trigger>
        <Menu.Content side="bottom" align="end" sideOffset={4} minWidth={200}>
          {!confirmingDelete && !confirmingDeleteData ? (
            <>
              <Menu.Item
                onSelect={() => onPin?.(!pinned)}
                className="drawer__menu-item--icon"
              >
                {pinned
                  ? <Pin width={14} height={14} aria-hidden="true" />
                  : <PinFilled width={14} height={14} aria-hidden="true" />}
                <span>{pinned ? 'Unpin' : 'Pin to top'}</span>
              </Menu.Item>
              <Menu.Item onSelect={() => onRenameStart()}>Rename</Menu.Item>
              {onOpenInTab && (
                // Pin this chat/app as a tab in the shell strip so the owner
                // can swap to it with one tap. Closes the menu first (same
                // reason as Delete below — the row can slide as the strip
                // renders).
                <Menu.Item onSelect={() => { onMenuToggle(false); onOpenInTab() }}>
                  Open in tab
                </Menu.Item>
              )}
              {kind === 'app' && slug && (
                // Opens the in-PWA InstallSheet to set the home-screen
                // name + icon first; the sheet saves, then navigates
                // same-tab to `/apps/<slug>/?install=1`. Same-tab keeps
                // the user in the installed Möbius PWA context — no
                // jarring browser-tab pop-out — and lets engagement
                // from the parent shell count toward the per-origin
                // Site Engagement score that gates beforeinstallprompt.
                <Menu.Item onSelect={() => onInstall?.()}>
                  Install to home screen
                </Menu.Item>
              )}
              {kind === 'chat' ? (
                // Chats soft-delete with 7-day recovery, so no
                // confirm step — one tap deletes, the note below
                // tells the user how to undo via the agent.
                // Close the parent's menu state BEFORE onDelete fires:
                // the row unmounts as soon as the refetch lands, but
                // the parent's openMenu still references this row's
                // id, leaving a Radix trigger looking "pressed" on
                // whichever row slides up into the slot.
                <Menu.Item
                  onSelect={() => { onMenuToggle(false); onDelete() }}
                  className="drawer__menu-item--danger"
                >
                  Delete
                </Menu.Item>
              ) : (
                // Deleting an app is a reversible soft-delete (the agent
                // can recover it for 7 days, like a chat), but we still
                // want a confirm step. `preventDefault` on onSelect stops
                // Radix from auto-closing the menu when the item is
                // selected — we want the menu to stay open and swap
                // to the confirm-chip below.
                <Menu.Item
                  onSelect={(e) => { e.preventDefault(); setConfirmingDelete(true) }}
                  className="drawer__menu-item--danger"
                >
                  Delete
                </Menu.Item>
              )}
              {kind === 'app' && (
                // Wipes the app's stored data but keeps it installed — a
                // separate action from Delete (which removes the whole app).
                // Same preventDefault-to-hold-open + confirm-chip pattern as
                // the app Delete above; the wording stays exactly "Delete
                // data" (no "keeps your data" phrasing).
                <Menu.Item
                  onSelect={(e) => { e.preventDefault(); setConfirmingDeleteData(true) }}
                  className="drawer__menu-item--danger"
                >
                  Delete data
                </Menu.Item>
              )}
            </>
          ) : confirmingDeleteData ? (
            <div className="drawer__menu-confirm">
              <span className="drawer__menu-confirm-label">Delete data?</span>
              <div className="drawer__menu-confirm-actions">
                <button
                  type="button"
                  className="drawer__menu-confirm-btn drawer__menu-confirm-btn--cancel"
                  onClick={() => setConfirmingDeleteData(false)}
                >
                  Cancel
                </button>
                <button
                  type="button"
                  className="drawer__menu-confirm-btn drawer__menu-confirm-btn--yes"
                  onClick={() => {
                    // Close the parent's menu state before the wipe fires,
                    // matching the Delete confirm below — keeps Radix's
                    // open-trigger bookkeeping in sync. The app STAYS in the
                    // list here (only its data is wiped), so no row unmounts.
                    setConfirmingDeleteData(false)
                    onMenuToggle(false)
                    onDeleteData?.()
                  }}
                >
                  Delete data
                </button>
              </div>
            </div>
          ) : (
            <div className="drawer__menu-confirm">
              <span className="drawer__menu-confirm-label">Confirm delete?</span>
              <div className="drawer__menu-confirm-actions">
                <button
                  type="button"
                  className="drawer__menu-confirm-btn drawer__menu-confirm-btn--cancel"
                  onClick={() => setConfirmingDelete(false)}
                >
                  Cancel
                </button>
                <button
                  type="button"
                  className="drawer__menu-confirm-btn drawer__menu-confirm-btn--yes"
                  onClick={() => {
                    // Order matters. Closing the parent's openMenu
                    // state BEFORE the delete fires keeps Radix's
                    // open-trigger bookkeeping in sync with the row
                    // that's about to unmount. Without this, the
                    // three-dots button on whatever row slides up
                    // into the deleted slot looks stuck pressed
                    // because openMenu still references the dead id.
                    setConfirmingDelete(false)
                    onMenuToggle(false)
                    onDelete()
                  }}
                >
                  Delete
                </button>
              </div>
            </div>
          )}
          {/* The 7-day recovery note applies to Delete (soft-delete), not
              to the immediate, non-recoverable "Delete data" wipe — hide it
              while that confirm chip is showing. */}
          {!confirmingDeleteData && (
            <p className="drawer__menu-note">
              The agent can recover deleted {kind === 'chat' ? 'chats' : 'apps'} for 7 days.
            </p>
          )}
        </Menu.Content>
      </Menu>
    </div>
  )
}
