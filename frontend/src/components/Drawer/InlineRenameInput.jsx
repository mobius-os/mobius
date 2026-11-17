/* The one inline rename field for drawer rows, launcher cards, and project
   chat rows. Enter, tabbing away, a tap or click outside the field, and the
   app losing focus all save a changed name; Escape discards the edit. An
   empty or unchanged name saves nothing, and nothing saves mid-composition. */

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
  const composingRef = useRef(false)
  // Leaving the field mid-composition saves once the IME hands over the
  // finished text, not the half-composed string.
  const saveAfterCompositionRef = useRef(false)
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

  function saveTyped() {
    if (composingRef.current) {
      saveAfterCompositionRef.current = true
      return
    }
    settle(inputRef.current?.value)
  }
  latestRef.current = { saveTyped }

  useEffect(() => {
    // Select a frame later: focusing synchronously races the browser moving
    // focus off the just-unmounted menu item.
    const frame = requestAnimationFrame(() => {
      inputRef.current?.focus()
      inputRef.current?.select()
    })
    // A tap outside saves before it reaches its target, so the tapped row,
    // button, or drawer scrim then acts as usual and closing the drawer or
    // moving focus cannot lose the typed name.
    function onOutsidePointer(event) {
      finishMenuFocusHandoff()
      const input = inputRef.current
      if (!input || input.contains(event.target)) return
      latestRef.current.saveTyped()
    }
    // Switching windows or apps leaves the field as the page's active
    // element, so its own blur settles nothing, and a hidden page may never
    // come back. The window also blurs when focus moves into an app frame.
    function onWindowBlur() {
      latestRef.current.saveTyped()
    }
    function onVisibilityChange() {
      if (document.visibilityState === 'hidden') latestRef.current.saveTyped()
    }
    document.addEventListener('pointerdown', onOutsidePointer, true)
    window.addEventListener('blur', onWindowBlur)
    document.addEventListener('visibilitychange', onVisibilityChange)
    return () => {
      cancelAnimationFrame(frame)
      document.removeEventListener('pointerdown', onOutsidePointer, true)
      window.removeEventListener('blur', onWindowBlur)
      document.removeEventListener('visibilitychange', onVisibilityChange)
    }
  }, [])

  function onKeyDown(event) {
    finishMenuFocusHandoff()
    // Enter and Escape also confirm or dismiss an IME candidate. Safari sends
    // that confirming key after compositionend, marked only by keyCode 229.
    if (event.nativeEvent?.isComposing || event.nativeEvent?.keyCode === 229) return
    if (event.key === 'Enter') {
      event.preventDefault()
      settle(event.currentTarget.value)
    } else if (event.key === 'Escape') {
      event.preventDefault()
      settle(null)
    }
  }

  function onCompositionEnd() {
    composingRef.current = false
    if (!saveAfterCompositionRef.current) return
    saveAfterCompositionRef.current = false
    saveTyped()
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
      saveTyped()
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
      onCompositionStart={() => { composingRef.current = true }}
      onCompositionEnd={onCompositionEnd}
      onBlur={onBlur}
    />
  )
}
