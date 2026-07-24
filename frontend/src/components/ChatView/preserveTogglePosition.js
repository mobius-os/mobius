export function preserveTogglePosition(anchorEl, bodyEl = anchorEl?.nextElementSibling) {
  if (!anchorEl || typeof requestAnimationFrame !== 'function') return
  const scroller = anchorEl.closest?.('.chat__scroll')
  if (!scroller) return

  // FOLLOW_BOTTOM is already a complete, idempotent layout policy: the scroll
  // controller follows the new real-content tail after every ResizeObserver
  // pass. A second header-local correction would race that authority and make
  // identical toggles sometimes hold and sometimes move. Outside follow mode,
  // preserve the reader's exact header position below.
  if (scroller.dataset?.scrollMode === 'FOLLOW_BOTTOM') return

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

  if (typeof MutationObserver === 'function' && bodyEl) {
    observer = new MutationObserver(settle)
    // Every disclosure keeps its body node mounted and flips `hidden` while
    // inserting/removing its rendered children. Watching the header's parent
    // child list never saw that transition, leaving the rAF fallback to race a
    // ResizeObserver. The body's own hidden attribute is the exact commit
    // boundary, independent of live descendant churn.
    observer.observe(bodyEl, {
      attributes: true,
      attributeFilter: ['hidden'],
    })
  }

  // Safety fallback for an unusual disclosure that changes layout without a
  // child-list mutation. When the observer fired, this becomes a cheap no-op.
  requestAnimationFrame(settle)
}
