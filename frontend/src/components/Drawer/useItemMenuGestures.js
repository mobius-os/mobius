/* Right-click, keyboard (ContextMenu, Shift+F10), and touch-hold gestures that
   open one item's action menu, shared by drawer rows, launcher cards, and
   project chat rows. */

import { useEffect, useRef } from 'react'
import { PRESS_MENU_HOLD_MS, PRE_HOLD_MOVE_PX } from '../Shell/dragController.js'

const TOUCH_CONTEXT_MENU_PROVENANCE_MS = 1500

function isMenuKey(event) {
  return event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')
}

// Where an item's action menu opens. Pointer gestures use their real release
// point; keyboard invocations report 0,0 and fall back to the bottom-center of
// the item they were invoked on.
export function itemMenuPlacement(point, trigger) {
  const rect = trigger?.getBoundingClientRect?.()
  const hasPoint = Number.isFinite(Number(point?.x))
    && Number.isFinite(Number(point?.y))
    && (Number(point.x) !== 0 || Number(point.y) !== 0)
  return {
    clientX: hasPoint ? Number(point.x) : (rect?.left || 0) + (rect?.width || 0) / 2,
    clientY: hasPoint ? Number(point.y) : rect?.bottom || 0,
  }
}

// `openMenuAt(point, trigger, { focusFirstAction })` places the menu. Pointer
// gestures pass their real point; keyboard invocations pass the event's 0,0,
// which the caller replaces with its trigger's position.
export default function useItemMenuGestures(openMenuAt) {
  const openMenuAtRef = useRef(openMenuAt)
  openMenuAtRef.current = openMenuAt
  const holdTimerRef = useRef(null)
  const holdOriginRef = useRef(null)
  const suppressHeldClickRef = useRef(false)
  const secondaryReleaseCleanupRef = useRef(null)
  // iOS may dispatch its long-press contextmenu AFTER pointercancel. Keep the
  // touch provenance independent of the live pointer session so that delayed
  // native event cannot open actions before a release. It expires, and keyboard
  // or mouse input clears it, so accessibility and desktop context menus remain.
  const touchMenuPointerAtRef = useRef(0)

  useEffect(() => () => {
    if (holdTimerRef.current) clearTimeout(holdTimerRef.current)
    secondaryReleaseCleanupRef.current?.()
  }, [])

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
    // Touch menus open only from a stationary hold. Native contextmenu would
    // steal that timing and re-open actions on release, so suppress it; mouse
    // and keyboard remain semantic.
    if (suppressTouchContextMenu(event)) return
    event.preventDefault()
    event.stopPropagation()
    // Chromium raises mouse contextmenu on secondary-button DOWN. The matching
    // UP below owns that gesture and opens only after release, so focus cannot
    // snap back to the source or select a collision-flipped menu item.
    if (event.type === 'contextmenu' && secondaryReleaseCleanupRef.current) return
    openMenuAtRef.current({
      x: event.clientX,
      y: event.clientY,
    }, event.currentTarget, {
      focusFirstAction: event.type === 'keydown',
    })
  }

  function openMenuFromKey(event) {
    touchMenuPointerAtRef.current = 0
    if (isMenuKey(event)) openItemMenu(event)
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
      openMenuAtRef.current(openingPoint, sourceBtn)
    }
    window.addEventListener('pointerup', onSecondaryPointerUp, true)
    window.addEventListener('pointercancel', cleanup, true)
    window.addEventListener('blur', cleanup, true)
    timer = setTimeout(cleanup, 1500)
    secondaryReleaseCleanupRef.current = cleanup
    return true
  }

  function beginHold(event) {
    if (event.pointerType === 'mouse' || event.button !== 0) return
    if (holdTimerRef.current) clearTimeout(holdTimerRef.current)
    const trigger = event.currentTarget
    holdOriginRef.current = { x: event.clientX, y: event.clientY }
    holdTimerRef.current = setTimeout(() => {
      holdTimerRef.current = null
      suppressHeldClickRef.current = true
      openMenuAtRef.current(holdOriginRef.current, trigger)
    }, PRESS_MENU_HOLD_MS)
  }

  function cancelHold() {
    if (holdTimerRef.current) clearTimeout(holdTimerRef.current)
    holdTimerRef.current = null
    holdOriginRef.current = null
  }

  // The release that ends a menu-opening hold still produces a click; it must
  // not also open the item.
  function consumeHeldClick() {
    if (!suppressHeldClickRef.current) return false
    suppressHeldClickRef.current = false
    return true
  }

  // A standalone trigger owns every gesture itself. Drawer rows instead hand
  // touch holds to the drawer's shared row controller and use the pieces.
  const triggerProps = {
    onContextMenuCapture: suppressTouchContextMenu,
    onContextMenu: openItemMenu,
    onPointerDown(event) {
      recordItemMenuPointer(event)
      if (beginSecondaryMenuPress(event)) return
      beginHold(event)
    },
    onPointerUp: cancelHold,
    onPointerCancel: cancelHold,
    onPointerMove(event) {
      const origin = holdOriginRef.current
      if (origin && Math.hypot(event.clientX - origin.x, event.clientY - origin.y) > PRE_HOLD_MOVE_PX) {
        cancelHold()
      }
    },
    onKeyDown: openMenuFromKey,
  }

  return {
    triggerProps,
    consumeHeldClick,
    openItemMenu,
    openMenuFromKey,
    recordItemMenuPointer,
    beginSecondaryMenuPress,
  }
}
