import { useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Plus, Chats, Grid, DotsVerticalMoreMenu, SettingsCog, Pin, PinFilled, Download, XCrossed } from '@openai/apps-sdk-ui/components/Icon'
import { Menu } from '@openai/apps-sdk-ui/components/Menu'
import { EmptyMessage } from '@openai/apps-sdk-ui/components/EmptyMessage'
import { apiFetch } from '../../api/client.js'
import { appQueries, chatQueries } from '../../hooks/queries.js'
import './Drawer.css'

// Module-level constant so the default for `streamingChatIds` is
// stable across renders. A fresh `new Set()` per call would break the
// `streamingSet.has(id)` identity-based memoization downstream.
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
  onNewChat,
  onDeleteChat,
  onSettings,
  // Set of chat ids whose agent is currently streaming. Used to
  // pulse a small accent dot next to the row label so the user can
  // see at a glance which background builds are still running.
  // Sourced from Shell (the only place that knows when a turn is
  // active across the whole app). Defaults to an empty Set so the
  // drawer renders cleanly if no parent supplies the prop.
  streamingChatIds,
  // The captured `beforeinstallprompt` event if the PWA can be
  // installed AND the user hasn't dismissed the prompt this session.
  // null when there's no install affordance to surface.
  pwaPrompt,
  onPwaInstall,
  onPwaDismiss,
  // Truthy when any registered provider's refresh token is no longer
  // valid. Drives a small warning dot on the Settings row — passive
  // nudge toward Reconnect, no modal, no banner.
  settingsWarning,
}) {
  const streamingSet = streamingChatIds || EMPTY_SET
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

          </div>{/* /.drawer__scroll-wrap */}

          <div className="drawer__group drawer__group--bottom">
            {/* PWA install card. Renders only when (a) the browser
                fired beforeinstallprompt and Shell stashed the
                deferred event, and (b) the user hasn't dismissed it
                (Shell's effect checks the same localStorage key).
                Shape rhymes with .drawer__item--new — tinted bg,
                accent border, rounded — so it reads as an actionable
                affordance rather than a notification. The previous
                fixed-position banner pinned bottom-of-screen was
                visually noisy and unrecoverable once dismissed; this
                card stays in the drawer where the user looks
                deliberately. */}
            {pwaPrompt && (
              <div className="drawer__pwa-card">
                <div className="drawer__pwa-card-row">
                  <Download width={16} height={16} aria-hidden="true" />
                  <span className="drawer__pwa-card-text">Install Möbius</span>
                </div>
                <div className="drawer__pwa-card-actions">
                  <button
                    type="button"
                    className="drawer__pwa-btn drawer__pwa-btn--install"
                    onClick={onPwaInstall}
                  >
                    Install
                  </button>
                  <button
                    type="button"
                    className="drawer__pwa-btn drawer__pwa-btn--dismiss"
                    onClick={onPwaDismiss}
                    aria-label="Dismiss install prompt"
                  >
                    <XCrossed width={14} height={14} aria-hidden="true" />
                  </button>
                </div>
              </div>
            )}
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
  const inputRef = useRef(null)
  const [confirmingDelete, setConfirmingDelete] = useState(false)

  // Reset the inline-confirm two-step (apps only) whenever the menu
  // closes — otherwise reopening would land the user back on the
  // primed "Confirm delete?" view. The SDK Menu (Radix) handles
  // open/close, outside-click, escape, and collision-aware
  // positioning natively; we just listen for the close.
  useEffect(() => {
    if (!menuOpen) setConfirmingDelete(false)
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
        {/* Streaming pulse dot. Sits before the text so the user's eye
            picks it up alongside the label rather than at the row's
            edge (where the pin lives). aria-label exposes the state to
            assistive tech; the dot itself is presentational. */}
        {streaming && (
          <span
            className="drawer__streaming-dot"
            aria-label="Currently streaming"
            title="Currently streaming"
          />
        )}
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
          {!confirmingDelete ? (
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
              {kind === 'app' && slug && (
                // Always open the standalone surface in a new browser
                // tab (`_blank`). From inside the installed Möbius
                // PWA, Chromium's installed-app registry remembers
                // Möbius covers this origin and suppresses
                // `beforeinstallprompt` for any same-origin sub-PWA.
                // The only reliable install path is Chrome's own
                // ⋮ → "Add to Home screen" menu — which requires
                // the user to be in a real browser tab where that
                // menu exists. `_blank` from an installed PWA pops
                // to system browser; from a regular tab it just
                // opens another tab. Either way the user lands in a
                // context with browser chrome available.
                <Menu.Item
                  onSelect={() => {
                    window.open(`/apps/${slug}/?install=1`, '_blank', 'noopener');
                  }}
                >
                  Install to home screen
                </Menu.Item>
              )}
              {kind === 'chat' ? (
                // Chats soft-delete with 7-day recovery, so no
                // confirm step — one tap deletes, the note below
                // tells the user how to undo via the agent.
                <Menu.Item
                  onSelect={() => onDelete()}
                  className="drawer__menu-item--danger"
                >
                  Delete
                </Menu.Item>
              ) : (
                // Apps are hard-deleted (no recovery), so we need a
                // confirm step. `preventDefault` on onSelect stops
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
            </>
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
                  onClick={() => { setConfirmingDelete(false); onDelete() }}
                >
                  Delete
                </button>
              </div>
            </div>
          )}
          {kind === 'chat' && (
            <p className="drawer__menu-note">
              The agent can recover deleted chats for 7 days.
            </p>
          )}
        </Menu.Content>
      </Menu>
    </div>
  )
}
