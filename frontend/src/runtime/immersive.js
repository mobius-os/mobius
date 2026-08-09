// window.mobius.immersive — app-driven control of the Möbius shell top bar.
//
// Hiding the top bar lets an app that already has its OWN header take the whole
// pane, so the owner never sees two stacked toolbars. This is the friendly
// wrapper over the `moebius:immersive` postMessage protocol; the shell-side
// state contract lives in src/lib/immersive.js and AppCanvas.jsx.
//
// The applied state is authoritative from the SHELL, not the app: AppCanvas
// echoes `moebius:immersive-state` on every applied change — the app's own
// request, the shell's floating exit button, and an app switch that releases
// the lease (immersive is deliberately sessional). The helper mirrors that echo
// so `toggle()` and `.hidden` stay correct even when the shell released the bar
// on its own. Trust is the sender check `event.source === window.parent`
// (matching navigation.js), because the opaque frame's origin string is not
// reliable.
//
// Standalone `/apps/<slug>/` has no Möbius top bar, so the request is harmlessly
// ignored there — the same app code is portable across both hosts.

const HOLD_MS = 450
const MOVE_TOL_PX = 10

export function makeImmersive({ appId } = {}) {
  let hidden = false
  const listeners = new Set()
  const hasParent = typeof window !== 'undefined'
    && window.parent && window.parent !== window

  function notify() {
    for (const cb of [...listeners]) {
      try { cb(hidden) } catch (e) {}
    }
  }

  // Shell → app: the authoritative applied verdict for THIS app.
  if (typeof window !== 'undefined' && hasParent) {
    window.addEventListener('message', (e) => {
      if (e.source !== window.parent) return
      const msg = e.data
      if (!msg || msg.type !== 'moebius:immersive-state') return
      const next = msg.value === true
      if (next === hidden) return
      hidden = next
      notify()
    })
  }

  function post(value) {
    // The documented immersive channel targets '*': the opaque frame cannot
    // name the parent origin reliably (see building-apps.md).
    // mode:'bar' asks the shell for the lighter "look like a standalone app"
    // collapse — hide the Möbius toolbar but keep the status-bar/notch strip so
    // the app's own header sits below it (NOT the full-bleed under-the-notch
    // takeover a game gets with the default mode:'full').
    try {
      window.parent.postMessage(
        { type: 'moebius:immersive', value: !!value, mode: 'bar', appId }, '*',
      )
    } catch (e) {}
  }

  // set(true) hides the Möbius top bar; set(false) restores it. Optimistic —
  // the shell echo confirms or corrects. Returns the resulting hidden state.
  function set(nextHidden) {
    const v = !!nextHidden
    if (v !== hidden) { hidden = v; notify() }
    if (hasParent) post(v)
    return hidden
  }

  function toggle() { return set(!hidden) }

  // cb(hidden) fires immediately with the current value and again on change.
  // Returns an unsubscribe function.
  function subscribe(cb) {
    if (typeof cb !== 'function') return () => {}
    listeners.add(cb)
    try { cb(hidden) } catch (e) {}
    return () => { listeners.delete(cb) }
  }

  // Wire a press-AND-HOLD on an element (the app's own top-left logo) to toggle
  // full-width. HOLD ONLY — a plain click never toggles, so an accidental tap on
  // the logo can't collapse the app. Idempotent: safe to call on every React
  // render via a callback ref — `ref={el => window.mobius.immersive.holdToToggle(el)}`
  // — the second call returns the same cleanup instead of double-wiring. Returns
  // a cleanup function.
  function holdToToggle(el, opts = {}) {
    if (!el || typeof el.addEventListener !== 'function') return () => {}
    if (el.__mobiusHold) return el.__mobiusHold
    const holdMs = typeof opts.holdMs === 'number' ? opts.holdMs : HOLD_MS
    let timer = null
    let startX = 0
    let startY = 0
    let fired = false
    let pointerId = null

    // Make the element a reliable long-press target on touch. Without
    // `touch-action: none` the browser claims the touch for scrolling and fires
    // pointercancel before the hold completes — the "holding does nothing on my
    // phone" bug. We also suppress the iOS image/callout + text selection a
    // long-press would otherwise raise, and show a pointer cursor so the logo
    // reads as interactive. Saved/restored so cleanup is clean.
    const prev = {
      cursor: el.style.cursor,
      touchAction: el.style.touchAction,
      userSelect: el.style.userSelect,
      webkitUserSelect: el.style.webkitUserSelect,
      webkitTouchCallout: el.style.webkitTouchCallout,
    }
    el.style.cursor = 'pointer'
    el.style.touchAction = 'none'
    el.style.userSelect = 'none'
    el.style.webkitUserSelect = 'none'
    el.style.webkitTouchCallout = 'none'

    function clearTimer() {
      if (timer !== null) { clearTimeout(timer); timer = null }
    }
    function release() {
      clearTimer()
      if (pointerId !== null) {
        try { el.releasePointerCapture(pointerId) } catch (e2) {}
        pointerId = null
      }
    }
    function onPointerDown(e) {
      if (e.pointerType === 'mouse' && e.button !== 0) return
      fired = false
      startX = e.clientX
      startY = e.clientY
      pointerId = e.pointerId
      // Capture so a slight finger drift keeps sending events here and the
      // gesture can't be silently handed to an ancestor.
      try { el.setPointerCapture(e.pointerId) } catch (e2) {}
      clearTimer()
      timer = setTimeout(() => {
        timer = null
        fired = true
        try { if (navigator.vibrate) navigator.vibrate(10) } catch (e2) {}
        toggle()
      }, holdMs)
    }
    function onPointerMove(e) {
      if (timer === null) return
      if (Math.abs(e.clientX - startX) > MOVE_TOL_PX
        || Math.abs(e.clientY - startY) > MOVE_TOL_PX) clearTimer()
    }
    function onPointerEnd() { release() }
    function onClickCapture(e) {
      if (!fired) return
      // A hold just toggled; don't also fire the logo's own click.
      e.preventDefault()
      e.stopPropagation()
      fired = false
    }
    function onContextMenu(e) {
      // Touch long-press raises the native context menu — always suppress it on
      // this element so the press reads as our gesture.
      e.preventDefault()
    }

    el.addEventListener('pointerdown', onPointerDown)
    el.addEventListener('pointermove', onPointerMove)
    el.addEventListener('pointerup', onPointerEnd)
    el.addEventListener('pointercancel', onPointerEnd)
    el.addEventListener('click', onClickCapture, true)
    el.addEventListener('contextmenu', onContextMenu)

    const cleanup = () => {
      release()
      delete el.__mobiusHold
      el.style.cursor = prev.cursor
      el.style.touchAction = prev.touchAction
      el.style.userSelect = prev.userSelect
      el.style.webkitUserSelect = prev.webkitUserSelect
      el.style.webkitTouchCallout = prev.webkitTouchCallout
      el.removeEventListener('pointerdown', onPointerDown)
      el.removeEventListener('pointermove', onPointerMove)
      el.removeEventListener('pointerup', onPointerEnd)
      el.removeEventListener('pointercancel', onPointerEnd)
      el.removeEventListener('click', onClickCapture, true)
      el.removeEventListener('contextmenu', onContextMenu)
    }
    el.__mobiusHold = cleanup
    return cleanup
  }

  return {
    set,
    toggle,
    subscribe,
    holdToToggle,
    get hidden() { return hidden },
  }
}
