export function preserveTogglePosition(anchorEl) {
  if (!anchorEl || typeof requestAnimationFrame !== 'function') return
  const scroller = anchorEl.closest?.('.chat__scroll')
  if (!scroller) return
  const before = anchorEl.getBoundingClientRect().top

  // A disclosure inserts/removes its body during React's click commit. Observe
  // that DOM mutation so the scroll correction lands in the same frame, before
  // the browser paints the collapsed layout. The old rAF-only correction let
  // one intermediate frame escape first: the page visibly moved with the
  // collapse, then moved back when rAF adjusted scrollTop.
  let settled = false
  let observer = null
  const settle = () => {
    if (settled) return
    settled = true
    observer?.disconnect()
    const after = anchorEl.getBoundingClientRect().top
    const delta = after - before
    if (Math.abs(delta) > 0.5) scroller.scrollTop += delta
  }

  if (typeof MutationObserver === 'function' && anchorEl.parentElement) {
    observer = new MutationObserver(settle)
    observer.observe(anchorEl.parentElement, { childList: true, subtree: true })
  }

  // Safety fallback for an unusual disclosure that changes layout without a
  // child-list mutation. When the observer fired, this becomes a cheap no-op.
  requestAnimationFrame(settle)
}
