/* Where a passive block is relative to the viewport. `near` (within a 400 px
   prefetch margin) decides whether it may have a hidden frame; `distance`
   (from the viewport's centre) ranks blocks competing for an app's idle
   slots. The distance is measured, not remembered from when the block crossed
   the margin: on entry and then at most once per animation frame while the
   page scrolls or resizes. */
import { useEffect, useState } from 'react'

export const NEAR_MARGIN = '400px 0px'

export function viewportDistance(rect, viewportHeight) {
  return Math.round(Math.abs((rect.top + rect.bottom) / 2 - viewportHeight / 2))
}

/** A callback ref, so the observer follows whichever section is mounted. */
export function useNearViewport() {
  const [node, setNode] = useState(null)
  const [near, setNear] = useState(false)
  const [distance, setDistance] = useState(Infinity)
  useEffect(() => {
    if (!node || typeof IntersectionObserver === 'undefined') return undefined
    const observer = new IntersectionObserver(entries => setNear(entries.at(-1).isIntersecting), { rootMargin: NEAR_MARGIN })
    observer.observe(node)
    return () => observer.disconnect()
  }, [node])
  useEffect(() => {
    if (!node || !near) return undefined
    let frame = null
    const measure = () => {
      frame = null
      setDistance(viewportDistance(node.getBoundingClientRect(), window.innerHeight))
    }
    const schedule = () => { if (frame === null) frame = requestAnimationFrame(measure) }
    measure()
    window.addEventListener('scroll', schedule, { capture: true, passive: true })
    window.addEventListener('resize', schedule, { passive: true })
    return () => {
      if (frame !== null) cancelAnimationFrame(frame)
      window.removeEventListener('scroll', schedule, { capture: true })
      window.removeEventListener('resize', schedule)
    }
  }, [node, near])
  return [setNode, { near, distance: near ? distance : Infinity }]
}
