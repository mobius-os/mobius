import { useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Plus, Chats, Grid, DotsVerticalMoreMenu, SettingsCog, Pin, PinFilled } from '@openai/apps-sdk-ui/components/Icon'
import { apiFetch } from '../../api/client.js'
import { appQueries, chatQueries } from '../../hooks/queries.js'
import './Drawer.css'

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
  onNewChat,
  onDeleteChat,
  onSettings,
}) {
  // Pinned-first sort: pinned rows by pinned_at desc, then unpinned
  // by updated_at desc. Server returns this order already (see
  // routes/chats.py list_chats), but we re-sort defensively so the
  // drawer stays correct if the cache holds an older response.
  const allChats = (chats || [])
    .filter(c => c.has_messages)
    .sort((a, b) => {
      const ap = a.pinned_at, bp = b.pinned_at
      if (ap && !bp) return -1
      if (!ap && bp) return 1
      if (ap && bp) return bp.localeCompare(ap)
      return (b.updated_at || '').localeCompare(a.updated_at || '')
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
  const [renamingState, setRenamingState] = useState(null) // { kind, id } | null

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
    const res = await apiFetch(`/chats/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ pinned }),
    })
    if (res.ok) refreshChats()
  }

  async function pinApp(id, pinned) {
    const res = await apiFetch(`/apps/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ pinned }),
    })
    if (res.ok) refreshApps()
  }

  async function deleteApp(id) {
    const res = await apiFetch(`/apps/${id}`, { method: 'DELETE' })
    if (res.ok || res.status === 404) refreshApps()
  }

  // Swipe-left-to-close. Mirror of the mobius-design-iter pattern:
  // touchstart captures origin, touchmove drags the panel 1:1 with
  // the finger when the gesture is dominantly horizontal-left,
  // touchend either closes (≥70px past origin AND horizontal-
  // dominant) or snaps back. The CSS transition is disabled mid-
  // drag via `drawer--dragging` so the panel tracks the finger
  // without easing.
  const drawerRef = useRef(null)
  const dragStart = useRef(null) // { x, y } or null

  function onTouchStart(e) {
    if (!open || e.touches.length !== 1) return
    dragStart.current = { x: e.touches[0].clientX, y: e.touches[0].clientY }
  }
  function onTouchMove(e) {
    if (!dragStart.current || e.touches.length !== 1) return
    const dx = e.touches[0].clientX - dragStart.current.x
    const dy = e.touches[0].clientY - dragStart.current.y
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
    if (el) {
      el.classList.remove('drawer--dragging')
      el.style.transform = ''
    }
    dragStart.current = null
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
    dragStart.current = null
  }

  return (
    <>
      <div
        className={`drawer-overlay ${open ? 'drawer-overlay--visible' : ''}`}
        onPointerDown={handleOverlayPointerDown}
        onClick={handleOverlayClick}
      />
      <nav
        ref={drawerRef}
        className={`drawer ${open ? 'drawer--open' : ''}`}
        aria-hidden={!open}
        inert={!open ? '' : undefined}
        onTouchStart={onTouchStart}
        onTouchMove={onTouchMove}
        onTouchEnd={onTouchEnd}
        onTouchCancel={onTouchCancel}
      >
        <div className="drawer__body">

          <button className="drawer__item drawer__item--new" onClick={onNewChat}>
            <span className="drawer__item-icon" aria-hidden="true">
              <Plus width={18} height={18} />
            </span>
            <span className="drawer__item-text">New chat</span>
          </button>

          <div className="drawer__group drawer__group--flex">
            <p className="drawer__label drawer__label--chats">
              <Chats width={16} height={16} aria-hidden="true" />
              <span>Chats</span>
            </p>
            <div className="drawer__scroll">
              {allChats.length > 0 ? allChats.map(chat => (
                <DrawerRow
                  key={chat.id}
                  kind="chat"
                  id={chat.id}
                  label={chat.title}
                  pinned={!!chat.pinned_at}
                  active={activeView === 'chat' && activeChatId === chat.id}
                  onSelect={() => onChat(chat.id)}
                  menuOpen={openMenu && openMenu.kind === 'chat' && openMenu.id === chat.id}
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
                  onDelete={() => onDeleteChat(chat.id)}
                />
              )) : (
                <p className="drawer__empty">No conversations yet</p>
              )}
            </div>
          </div>

          {apps.length > 0 && (
            <div className="drawer__group drawer__group--flex">
              <p className="drawer__label drawer__label--apps">
                <Grid width={16} height={16} aria-hidden="true" />
                <span>Apps</span>
              </p>
              <div className="drawer__scroll">
                {sortedApps.map(app => (
                  <DrawerRow
                    key={app.id}
                    kind="app"
                    id={app.id}
                    label={app.name}
                    pinned={!!app.pinned_at}
                    active={activeView === 'canvas' && Number(activeAppId) === Number(app.id)}
                    onSelect={() => onApp(app.id)}
                    menuOpen={openMenu && openMenu.kind === 'app' && openMenu.id === app.id}
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
                    onDelete={() => deleteApp(app.id)}
                  />
                ))}
              </div>
            </div>
          )}

          <div className="drawer__group drawer__group--bottom">
            <button
              className={`drawer__item ${activeView === 'settings' ? 'drawer__item--active' : ''}`}
              onClick={onSettings}
            >
              <SettingsCog width={16} height={16} aria-hidden="true" style={{ flexShrink: 0 }} />
              <span className="drawer__item-text">Settings</span>
            </button>
          </div>

        </div>
      </nav>
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
  onSelect,
  menuOpen,
  onMenuToggle,
  renaming,
  onRenameStart,
  onRenameCancel,
  onRenameSubmit,
  onPin,
  onDelete,
}) {
  const wrapRef = useRef(null)
  const triggerRef = useRef(null)
  const inputRef = useRef(null)
  const [confirmingDelete, setConfirmingDelete] = useState(false)
  // Flip-up when the row is near the bottom of the scroll viewport so
  // the menu doesn't clip. Measured on open from the row's rect vs
  // the nearest .drawer__scroll ancestor.
  const [flipUp, setFlipUp] = useState(false)

  // Reset the inline-confirm state every time the menu opens/closes so
  // the user doesn't reopen the menu and find the confirm chip still
  // primed from a previous open. Also re-measure flip-up direction.
  useEffect(() => {
    if (!menuOpen) {
      setConfirmingDelete(false)
      setFlipUp(false)
      return
    }
    // Measure on open: if there isn't ~180px below the row inside the
    // scroll container, flip the menu upward.
    const row = wrapRef.current
    const scroll = row?.closest('.drawer__scroll')
    if (!row || !scroll) return
    const rowRect = row.getBoundingClientRect()
    const scrollRect = scroll.getBoundingClientRect()
    const spaceBelow = scrollRect.bottom - rowRect.bottom
    setFlipUp(spaceBelow < 180)
  }, [menuOpen])

  // Outside-click + Escape close the menu. Matches the ComposerPopover
  // pattern (pointerdown + keydown listeners on document) so behavior
  // is consistent across popovers in the shell.
  useEffect(() => {
    if (!menuOpen) return
    function onPointer(e) {
      if (!wrapRef.current) return
      if (wrapRef.current.contains(e.target)) return
      onMenuToggle(false)
    }
    function onKey(e) {
      if (e.key === 'Escape') {
        onMenuToggle(false)
        triggerRef.current?.focus()
      }
    }
    document.addEventListener('pointerdown', onPointer)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onPointer)
      document.removeEventListener('keydown', onKey)
    }
  }, [menuOpen, onMenuToggle])

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
    <div
      className={`drawer__row${flipUp ? ' drawer__row--flip-up' : ''}`}
      ref={wrapRef}
    >
      <button
        type="button"
        className={`drawer__item ${active ? 'drawer__item--active' : ''}`}
        onClick={onSelect}
      >
        <span className="drawer__item-text">{label}</span>
        {pinned && (
          <span className="drawer__item-pin" aria-label="Pinned" title="Pinned">
            <PinFilled width={14} height={14} />
          </span>
        )}
      </button>
      <button
        ref={triggerRef}
        type="button"
        className="drawer__more"
        onClick={(e) => { e.stopPropagation(); onMenuToggle(!menuOpen) }}
        aria-label={`More actions for ${label}`}
        aria-haspopup="menu"
        aria-expanded={menuOpen}
      >
        <DotsVerticalMoreMenu width={16} height={16} aria-hidden="true" />
      </button>
      {menuOpen && (
        <div className="drawer__menu" role="menu">
          <button
            type="button"
            role="menuitem"
            className="drawer__menu-item drawer__menu-item--icon"
            onClick={() => { onMenuToggle(false); onPin?.(!pinned) }}
          >
            {pinned
              ? <Pin width={14} height={14} aria-hidden="true" />
              : <PinFilled width={14} height={14} aria-hidden="true" />}
            <span>{pinned ? 'Unpin' : 'Pin to top'}</span>
          </button>
          <button
            type="button"
            role="menuitem"
            className="drawer__menu-item"
            onClick={() => { onMenuToggle(false); onRenameStart() }}
          >
            Rename
          </button>
          {kind === 'chat' ? (
            // Chats soft-delete with 7-day recovery, so no confirm
            // step — one tap deletes, the note below tells the user
            // how to undo via the agent.
            <button
              type="button"
              role="menuitem"
              className="drawer__menu-item drawer__menu-item--danger"
              onClick={() => { onMenuToggle(false); onDelete() }}
            >
              Delete
            </button>
          ) : !confirmingDelete ? (
            <button
              type="button"
              role="menuitem"
              className="drawer__menu-item drawer__menu-item--danger"
              onClick={() => setConfirmingDelete(true)}
            >
              Delete
            </button>
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
                  onClick={() => { onMenuToggle(false); onDelete() }}
                >
                  Delete
                </button>
              </div>
            </div>
          )}
          {kind === 'chat' && (
            <p className="drawer__menu-note">
              Deleted chats stay recoverable by the agent for 7 days.
            </p>
          )}
        </div>
      )}
    </div>
  )
}
