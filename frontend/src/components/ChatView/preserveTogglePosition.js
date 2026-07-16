// A fast close→open can happen before ResizeObserver replaces the provisional
// collapse reservation. Remember the exact baseline so the next toggle can
// unwind only OUR still-present write instead of stacking another body height
// on top. WeakMap keeps the bookkeeping DOM-lifetime scoped.
const provisionalSpacer = new WeakMap()

function unwindProvisionalSpacer(spacer) {
  const pending = provisionalSpacer.get(spacer)
  if (!pending) return
  // If normal layout already wrote a different value, it owns the spacer now;
  // never roll that authoritative calculation back to our stale baseline.
  if (Math.abs((spacer.offsetHeight || 0) - pending.primedHeight) <= 1) {
    spacer.style.height = `${pending.baselineHeight}px`
  }
  provisionalSpacer.delete(spacer)
}

export function preserveTogglePosition(anchorEl) {
  if (!anchorEl || typeof requestAnimationFrame !== 'function') return
  const scroller = anchorEl.closest?.('.chat__scroll')
  if (!scroller) return

  // Closing a disclosure at the physical tail removes height before the chat's
  // ResizeObserver can grow its dynamic bottom reservation. The browser clamps
  // scrollTop in that gap, paints the header lower for one frame, then the
  // observer restores it — the visible down/up twitch. Reserve the body's exact
  // outer height synchronously so total scroll height stays constant across the
  // React commit; the observer replaces this provisional value with its normal
  // spacer calculation on the next layout pass.
  const spacer = scroller.querySelector?.('.spacer-dynamic')
  if (spacer) unwindProvisionalSpacer(spacer)

  if (anchorEl.getAttribute?.('aria-expanded') === 'true') {
    const body = anchorEl.nextElementSibling
    if (spacer && body) {
      const rectHeight = body.getBoundingClientRect?.().height || 0
      const styles = typeof getComputedStyle === 'function'
        ? getComputedStyle(body)
        : null
      const marginTop = Number.parseFloat(styles?.marginTop) || 0
      const marginBottom = Number.parseFloat(styles?.marginBottom) || 0
      const removedHeight = rectHeight + marginTop + marginBottom
      if (removedHeight > 0) {
        const baselineHeight = spacer.offsetHeight || 0
        const primedHeight = baselineHeight + removedHeight
        spacer.style.height = `${primedHeight}px`
        provisionalSpacer.set(spacer, { baselineHeight, primedHeight })
      }
    }
  }

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
