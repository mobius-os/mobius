/* Drawer mounts one retargetable action-menu controller so virtualized rows
   stay free of dormant menu hooks while scrolling. */

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  beginMenuPress,
  cancelMenuPress,
  consumeMenuClick,
  finishMenuPress,
} from './menuPointerOwnership.js'
import { Pin, PinFilled } from '@openai/apps-sdk-ui/components/Icon'
import { placeContextMenu } from '../../lib/contextMenuGeometry.js'
import useContextMenuOutsideDismiss from '../../hooks/useContextMenuOutsideDismiss.js'
import { captureLayoutSpace, clientPointToLayout } from '../../lib/layoutSpace.js'

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
  const pointerOwnerRef = useRef({ press: null, clickAction: null })
  const [confirmation, setConfirmation] = useState(null)
  const [position, setPosition] = useState(null)

  function close({ restoreFocus = true } = {}) {
    restoreOnCloseRef.current = restoreFocus
    onClose()
  }

  const closeFromOutside = useCallback(() => {
    pointerOwnerRef.current = { press: null, clickAction: null }
    restoreOnCloseRef.current = false
    onClose()
  }, [onClose])

  useContextMenuOutsideDismiss({
    open,
    menuRef,
    onDismiss: closeFromOutside,
  })

  useEffect(() => {
    if (open) {
      wasOpenRef.current = true
      return
    }
    pointerOwnerRef.current = { press: null, clickAction: null }
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
    const rootSpace = captureLayoutSpace(root)
    const menuRect = menuRef.current
    const placementX = Number(placement?.clientX)
    const placementY = Number(placement?.clientY)
    const point = Number.isFinite(placementX) && Number.isFinite(placementY)
      ? clientPointToLayout({ x: placementX, y: placementY }, rootSpace)
      : { x: rootSpace.width / 2, y: rootSpace.height / 2 }
    setPosition(placeContextMenu({
      point,
      viewport: { width: rootSpace.width, height: rootSpace.height },
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

  function actionFor(target) {
    return target?.closest?.('.drawer__item-action-item') || null
  }

  function blockReleaseThroughClick(event) {
    // A touch menu can appear underneath the contact that opened it. Android
    // may retarget that contact's trailing click to the newly mounted action,
    // even though no pointerdown ever began inside the menu. Only a click with
    // menu-owned press provenance may run; detail=0 preserves keyboard and
    // assistive-technology activation.
    const outcome = consumeMenuClick(pointerOwnerRef.current, {
      detail: event.detail,
      action: actionFor(event.target),
    })
    pointerOwnerRef.current = outcome.owner
    if (outcome.allowed) return
    event.preventDefault()
    event.stopPropagation()
    event.nativeEvent?.stopImmediatePropagation?.()
  }

  useEffect(() => {
    if (!open) return
    function clearAbandonedPointer(event) {
      if (menuRef.current?.contains(event.target)) return
      pointerOwnerRef.current = cancelMenuPress(
        pointerOwnerRef.current,
        event.pointerId,
      )
    }
    document.addEventListener('pointerup', clearAbandonedPointer, true)
    document.addEventListener('pointercancel', clearAbandonedPointer, true)
    return () => {
      document.removeEventListener('pointerup', clearAbandonedPointer, true)
      document.removeEventListener('pointercancel', clearAbandonedPointer, true)
    }
  }, [onClose, open])

  if (!open) return null

  const layer = (
    <div
      className="drawer__item-action-layer"
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
        onPointerDown={event => {
          pointerOwnerRef.current = beginMenuPress(pointerOwnerRef.current, {
            pointerId: event.pointerId,
            action: actionFor(event.target),
            isPrimary: event.isPrimary,
          })
          event.stopPropagation()
        }}
        onPointerUp={event => {
          pointerOwnerRef.current = finishMenuPress(pointerOwnerRef.current, {
            pointerId: event.pointerId,
            action: actionFor(event.target),
          })
        }}
        onPointerCancel={event => {
          pointerOwnerRef.current = cancelMenuPress(
            pointerOwnerRef.current,
            event.pointerId,
          )
        }}
        onClickCapture={blockReleaseThroughClick}
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
                // Rename opens an inline editor that focuses itself, so the menu
                // must not restore focus to the (now-unmounted) row trigger.
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
