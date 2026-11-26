// Swipe right from the frame's left edge to go back.
//
// Only hosts that know the device has no system back gesture (an installed iOS
// web app) turn this on, and only for a frame whose left edge is the screen's
// edge, never one beside a pane divider or sidebar. The gesture has no idea of
// its own about pages: navigation.js decides whether there is a level to leave
// and runs the same back as moebius:nav-back does for the top entry.

// A touch must start this close to the left edge, so horizontal scrollers in
// the app keep their own swipes.
const EDGE_PX = 28
// Release past this distance, or a quick flick past FLICK_PX, goes back.
const COMMIT_PX = 72
const FLICK_PX = 32
const FLICK_SPEED = 0.45 // px per ms
const DECIDE_PX = 8
const SIDEWAYS_DOMINANCE = 1.2
const TRAVEL_PX = 56 // how far the arrow follows the finger

// A modal dialog owns its own way out; the level beneath it must not close.
const OPEN_MODALS = 'dialog[open], [role="dialog"][aria-modal="true"], [role="alertdialog"][aria-modal="true"]'

const ARROW_STYLE = [
  'position:fixed', 'left:0', 'top:0', 'z-index:2147483647',
  'width:44px', 'height:44px', 'margin:-22px 0 0 -48px',
  'display:grid', 'place-items:center', 'border-radius:50%',
  'color:var(--text,#111)', 'background:var(--surface,#fff)',
  'border:1px solid var(--border,rgba(128,128,128,.4))',
  'box-shadow:0 6px 18px rgb(0 0 0 / .18)', 'pointer-events:none',
].join(';')
const ARROW_SVG = '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" '
  + 'stroke="currentColor" stroke-width="2.2" stroke-linecap="round" '
  + 'stroke-linejoin="round" aria-hidden="true"><path d="M15 18l-6-6 6-6"/></svg>'

function swipeCommits(dx, dy, ms) {
  if (dx <= 0 || dx < Math.abs(dy)) return false
  if (dx >= COMMIT_PX) return true
  return dx >= FLICK_PX && dx / Math.max(ms, 1) >= FLICK_SPEED
}

export function makeEdgeSwipeBack({ canGoBack, goBack }) {
  let doc = null
  let swipe = null
  let arrow = null
  let tracking = false
  const passive = { passive: true, capture: true }
  // Blocking listeners exist only while a touch that started in the edge zone
  // is down: one on the whole document would make every scroll in the app wait
  // for its main thread.
  const blocking = { passive: false, capture: true }

  const canStart = () => canGoBack() && !doc.querySelector(OPEN_MODALS)

  function place(dx, y) {
    if (!arrow) {
      arrow = doc.createElement('div')
      arrow.setAttribute('aria-hidden', 'true')
      arrow.style.cssText = ARROW_STYLE
      arrow.innerHTML = ARROW_SVG
      doc.body.appendChild(arrow)
    }
    const progress = Math.min(dx / COMMIT_PX, 1)
    arrow.style.top = `${y}px`
    arrow.style.opacity = String(Math.min(1, 0.25 + progress))
    arrow.style.transform = `translateX(${progress * TRAVEL_PX}px) scale(${0.8 + 0.2 * progress})`
  }

  function hide() {
    arrow?.remove()
    arrow = null
  }

  function track() {
    if (tracking) return
    tracking = true
    doc.addEventListener('touchmove', onMove, blocking)
    doc.addEventListener('touchend', onEnd, blocking)
    doc.addEventListener('touchcancel', onCancel, passive)
  }

  function untrack() {
    if (!tracking) return
    tracking = false
    doc.removeEventListener('touchmove', onMove, blocking)
    doc.removeEventListener('touchend', onEnd, blocking)
    doc.removeEventListener('touchcancel', onCancel, passive)
  }

  function abandon() {
    swipe = null
    untrack()
    hide()
  }

  function onStart(event) {
    const touch = event.touches.length === 1 ? event.touches[0] : null
    if (!touch || touch.clientX > EDGE_PX || !canStart()) {
      abandon()
      return
    }
    swipe = { x: touch.clientX, y: touch.clientY, t: event.timeStamp, sideways: false }
    track()
  }

  function onMove(event) {
    if (!swipe) return
    if (event.touches.length !== 1) {
      abandon()
      return
    }
    const touch = event.touches[0]
    const dx = touch.clientX - swipe.x
    const dy = touch.clientY - swipe.y
    if (!swipe.sideways) {
      if (Math.abs(dx) < DECIDE_PX && Math.abs(dy) < DECIDE_PX) return
      // Anything but a clear rightward drag stays the app's own touch.
      if (dx <= 0 || dx < Math.abs(dy) * SIDEWAYS_DOMINANCE) {
        abandon()
        return
      }
      swipe.sideways = true
    }
    if (event.cancelable) event.preventDefault()
    place(dx, touch.clientY)
  }

  function onEnd(event) {
    const current = swipe
    abandon()
    const touch = event.changedTouches[0]
    if (!current?.sideways || !touch) return
    if (event.cancelable) event.preventDefault()
    const dx = touch.clientX - current.x
    const dy = touch.clientY - current.y
    if (!swipeCommits(dx, dy, event.timeStamp - current.t)) return
    // The app may have closed the level or opened a dialog under the finger.
    if (canStart()) goBack()
  }

  function onCancel() {
    abandon()
  }

  return {
    setEnabled(enabled) {
      if (enabled === (doc !== null)) return
      if (enabled) {
        doc = globalThis.document
        doc.addEventListener('touchstart', onStart, passive)
        return
      }
      abandon()
      doc.removeEventListener('touchstart', onStart, passive)
      doc = null
    },
  }
}
