export function preserveTogglePosition(anchorEl) {
  if (!anchorEl || typeof requestAnimationFrame !== 'function') return
  const scroller = anchorEl.closest?.('.chat__scroll')
  if (!scroller) return
  const before = anchorEl.getBoundingClientRect().top
  requestAnimationFrame(() => {
    const after = anchorEl.getBoundingClientRect().top
    const delta = after - before
    if (Math.abs(delta) > 0.5) scroller.scrollTop += delta
  })
}
