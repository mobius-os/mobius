/* DrawerItemActionMenu gives app launcher cards and drawer rows one compact,
   pointer-accurate contextual menu across mouse, keyboard, and touch. */

import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Pin, PinFilled } from '@openai/apps-sdk-ui/components/Icon'
import { placeContextMenu } from '../../lib/contextMenuGeometry.js'

function focusableMenuItems(menu) {
  return [...(menu?.querySelectorAll('[role="menuitem"]:not([disabled])') || [])]
}

export default function DrawerItemActionMenu({
  open,
  itemKind,
  itemName,
  pinned,
  canInstall,
  canShare,
  placement,
  restoreFocusRef,
  onClose,
  onPin,
  onRename,
  onInstall,
  onShare,
  onDelete,
  onDeleteData,
}) {
  const menuRef = useRef(null)
  const wasOpenRef = useRef(false)
  const restoreOnCloseRef = useRef(true)
  const outsidePointerDownRef = useRef(null)
  const [confirmation, setConfirmation] = useState(null)
  const [position, setPosition] = useState(null)

  function close({ restoreFocus = true } = {}) {
    restoreOnCloseRef.current = restoreFocus
    onClose()
  }

  useLayoutEffect(() => {
    if (open) outsidePointerDownRef.current = null
  }, [open])

  useEffect(() => {
    if (open) {
      wasOpenRef.current = true
      return
    }
    setConfirmation(null)
    setPosition(null)
    if (!wasOpenRef.current) return
    wasOpenRef.current = false
    if (!restoreOnCloseRef.current) return
    const frame = requestAnimationFrame(() => restoreFocusRef?.current?.focus())
    return () => cancelAnimationFrame(frame)
  }, [open, restoreFocusRef])

  useEffect(() => {
    if (!open) return
    function closeFromKeyboard(event) {
      if (event.key !== 'Escape' && event.key !== 'Tab') return
      event.preventDefault()
      event.stopPropagation()
      event.stopImmediatePropagation()
      restoreOnCloseRef.current = true
      onClose()
    }
    document.addEventListener('keydown', closeFromKeyboard, true)
    return () => document.removeEventListener('keydown', closeFromKeyboard, true)
  }, [open, onClose])

  useLayoutEffect(() => {
    if (!open || !menuRef.current) return
    const root = document.documentElement
    const rootRect = root.getBoundingClientRect()
    const menuRect = menuRef.current
    const placementX = Number(placement?.clientX)
    const placementY = Number(placement?.clientY)
    setPosition(placeContextMenu({
      clientPoint: {
        x: Number.isFinite(placementX)
          ? placementX
          : rootRect.left + rootRect.width / 2,
        y: Number.isFinite(placementY)
          ? placementY
          : rootRect.top + rootRect.height / 2,
      },
      clientViewport: rootRect,
      menuSize: {
        width: menuRect.offsetWidth,
        height: menuRect.offsetHeight,
      },
    }))
  }, [open, placement, confirmation])

  useLayoutEffect(() => {
    if (!open || !position || !menuRef.current) return
    const frame = requestAnimationFrame(() => {
      focusableMenuItems(menuRef.current)[0]?.focus()
    })
    return () => cancelAnimationFrame(frame)
  }, [open, position, confirmation])

  if (!open) return null

  function run(action, { restoreFocus = true } = {}) {
    close({ restoreFocus })
    action()
  }

  function handleDeleteAction() {
    if (itemKind === 'chat') {
      run(onDelete, { restoreFocus: false })
      return
    }
    setConfirmation('delete')
  }

  function onMenuKeyDown(event) {
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return
    const items = focusableMenuItems(menuRef.current)
    if (!items.length) return
    event.preventDefault()
    const current = items.indexOf(document.activeElement)
    const next = event.key === 'Home'
      ? 0
      : event.key === 'End'
        ? items.length - 1
        : event.key === 'ArrowDown'
          ? (current + 1 + items.length) % items.length
          : (current - 1 + items.length) % items.length
    items[next].focus()
  }

  const recoveryLabel = itemKind === 'chat' ? 'chats' : 'apps'

  function consumeOutsidePointer(event) {
    if (event.target !== event.currentTarget) return false
    event.preventDefault()
    event.stopPropagation()
    event.nativeEvent?.stopImmediatePropagation?.()
    return true
  }

  const layer = (
    <div
      className="drawer__item-action-layer"
      onPointerDown={event => {
        // Keep the layer mounted for the complete tap. Closing on pointerdown
        // lets Android retarget the later release/click to the row underneath.
        if (consumeOutsidePointer(event)) {
          outsidePointerDownRef.current = event.pointerId
        }
      }}
      onPointerUp={event => {
        consumeOutsidePointer(event)
      }}
      onPointerCancel={event => {
        if (consumeOutsidePointer(event)) outsidePointerDownRef.current = null
      }}
      onClick={event => {
        if (!consumeOutsidePointer(event)) return
        // A hold can mount this layer between the source pointerup and its
        // browser-generated click. That click never began on the layer, so it
        // must not dismiss the menu it just opened. A deliberate outside tap
        // always contributes the layer-owned pointerdown recorded above.
        if (outsidePointerDownRef.current == null) return
        outsidePointerDownRef.current = null
        close()
      }}
      onContextMenu={event => event.preventDefault()}
      onWheel={event => {
        if (event.target === event.currentTarget) close()
      }}
    >
      <div
        ref={menuRef}
        className="drawer__item-action-menu"
        role="menu"
        aria-label={`${itemName} actions`}
        data-positioned={position ? 'true' : 'false'}
        style={{
          '--item-action-x': `${position?.x || 0}px`,
          '--item-action-y': `${position?.y || 0}px`,
        }}
        onPointerDown={event => event.stopPropagation()}
        onKeyDown={onMenuKeyDown}
      >
        {confirmation === 'delete-data' ? (
          <div className="drawer__item-action-confirm">
            <strong>Delete this app’s data?</strong>
            <span>The app stays installed, but its saved information is removed.</span>
            <div>
              <button
                type="button"
                role="menuitem"
                className="drawer__item-action-item"
                onClick={() => setConfirmation(null)}
              >
                Cancel
              </button>
              <button
                type="button"
                role="menuitem"
                className="drawer__item-action-item drawer__item-action-item--danger"
                onClick={() => run(onDeleteData)}
              >
                Delete data
              </button>
            </div>
          </div>
        ) : confirmation === 'delete' ? (
          <div className="drawer__item-action-confirm">
            <strong>Delete {itemName}?</strong>
            <span>The agent can recover deleted {recoveryLabel} for 7 days.</span>
            <div>
              <button
                type="button"
                role="menuitem"
                className="drawer__item-action-item"
                onClick={() => setConfirmation(null)}
              >
                Cancel
              </button>
              <button
                type="button"
                role="menuitem"
                className="drawer__item-action-item drawer__item-action-item--danger"
                onClick={() => run(onDelete, { restoreFocus: false })}
              >
                Delete {itemKind}
              </button>
            </div>
          </div>
        ) : (
          <>
            <div className="drawer__item-action-items">
              <button
                type="button"
                role="menuitem"
                className="drawer__item-action-item drawer__item-action-item--icon"
                onClick={() => run(onPin)}
              >
                {pinned
                  ? <Pin width={15} height={15} aria-hidden="true" />
                  : <PinFilled width={15} height={15} aria-hidden="true" />}
                <span>{pinned ? 'Unpin' : 'Pin'}</span>
              </button>
              <button
                type="button"
                role="menuitem"
                className="drawer__item-action-item"
                onClick={() => run(onRename, { restoreFocus: false })}
              >
                Rename
              </button>
              {canInstall && (
                <button
                  type="button"
                  role="menuitem"
                  className="drawer__item-action-item"
                  onClick={() => run(onInstall, { restoreFocus: false })}
                >
                  Install to home screen
                </button>
              )}
              {canShare && (
                <button
                  type="button"
                  role="menuitem"
                  className="drawer__item-action-item"
                  onClick={() => run(onShare, { restoreFocus: false })}
                >
                  Share app
                </button>
              )}
              <div className="drawer__item-action-separator" role="separator" />
              <button
                type="button"
                role="menuitem"
                className="drawer__item-action-item drawer__item-action-item--danger"
                onClick={handleDeleteAction}
              >
                Delete
              </button>
              {itemKind === 'app' && (
                <button
                  type="button"
                  role="menuitem"
                  className="drawer__item-action-item drawer__item-action-item--danger"
                  onClick={() => setConfirmation('delete-data')}
                >
                  Delete data
                </button>
              )}
            </div>
            <p className="drawer__item-action-note">
              The agent can recover deleted {recoveryLabel} for 7 days.
            </p>
          </>
        )}
      </div>
    </div>
  )

  return createPortal(layer, document.body)
}
