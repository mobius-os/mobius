/* The one inline rename field for drawer rows, launcher cards, and project
   chat rows. Enter or tabbing away saves a changed name; Escape or a tap
   outside the field discards the edit. */

import { useEffect, useRef } from 'react'

export default function InlineRenameInput({
  initialValue,
  maxLength,
  className,
  ariaLabel,
  onSubmit,
  onCancel,
}) {
  const inputRef = useRef(null)
  const settledRef = useRef(false)
  // Rename opens from the action menu, and closing that menu releases its
  // Back-stack entry with history.back(); the popstate drops focus to <body> a
  // frame later. That is the app moving focus, not the owner leaving the
  // field, so it is reclaimed once. The first pointer, key, or edit ends this
  // handoff, and every later blur settles normally.
  const menuFocusPendingRef = useRef(true)
  const latestRef = useRef(null)

  function finishMenuFocusHandoff() {
    menuFocusPendingRef.current = false
  }

  function settle(value) {
    if (settledRef.current) return
    settledRef.current = true
    const next = value?.trim()
    if (next && next !== initialValue) onSubmit(next)
    else onCancel()
  }
  latestRef.current = { settle }

  useEffect(() => {
    // Select a frame later: focusing synchronously races the browser moving
    // focus off the just-unmounted menu item.
    const frame = requestAnimationFrame(() => {
      inputRef.current?.focus()
      inputRef.current?.select()
    })
    // A tap outside cancels instead of letting another row, Settings, or New
    // chat act on it; the rename just exits. Both events are claimed:
    // pointerdown prevents the focus shift, and some browsers fire the click
    // regardless. The drawer scrim is the one exception: it stays a reliable
    // close surface, so its tap cancels and carries on to close the drawer.
    let swallowClick = false
    function onOutsidePointer(event) {
      finishMenuFocusHandoff()
      const input = inputRef.current
      if (!input || input.contains(event.target)) return
      latestRef.current.settle(null)
      if (event.target?.closest?.('.drawer-overlay')) return
      event.preventDefault()
      event.stopPropagation()
      swallowClick = true
    }
    function onOutsideClick(event) {
      if (!swallowClick) return
      swallowClick = false
      event.preventDefault()
      event.stopPropagation()
    }
    document.addEventListener('pointerdown', onOutsidePointer, true)
    document.addEventListener('click', onOutsideClick, true)
    return () => {
      cancelAnimationFrame(frame)
      document.removeEventListener('pointerdown', onOutsidePointer, true)
      document.removeEventListener('click', onOutsideClick, true)
    }
  }, [])

  function onKeyDown(event) {
    finishMenuFocusHandoff()
    // Enter and Escape also confirm or dismiss an IME candidate.
    if (event.nativeEvent?.isComposing) return
    if (event.key === 'Enter') {
      event.preventDefault()
      settle(event.currentTarget.value)
    } else if (event.key === 'Escape') {
      event.preventDefault()
      settle(null)
    }
  }

  function onBlur() {
    const input = inputRef.current
    requestAnimationFrame(() => {
      if (settledRef.current) return
      const active = document.activeElement
      if (active === input) return
      const focusDropped = !active
        || active === document.body
        || active === document.documentElement
      if (menuFocusPendingRef.current && focusDropped && input?.isConnected) {
        finishMenuFocusHandoff()
        input.focus()
        return
      }
      settle(input?.value)
    })
  }

  return (
    <input
      ref={inputRef}
      className={className}
      defaultValue={initialValue || ''}
      maxLength={maxLength}
      aria-label={ariaLabel}
      onChange={finishMenuFocusHandoff}
      onKeyDown={onKeyDown}
      onBlur={onBlur}
    />
  )
}
