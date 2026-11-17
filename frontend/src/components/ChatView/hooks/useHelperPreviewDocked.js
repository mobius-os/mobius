/* Dock a helper preview only when its owning chat has room for both columns. */
import { useLayoutEffect, useState } from 'react'

// The narrowest chat pane that still reads well as two side-by-side columns.
export const DOCKED_MIN_WIDTH = 1000

const wideEnough = host => (host?.clientWidth || 0) >= DOCKED_MIN_WIDTH

export default function useHelperPreviewDocked(host) {
  const [docked, setDocked] = useState(() => wideEnough(host))
  useLayoutEffect(() => {
    if (!host || typeof ResizeObserver === 'undefined') return undefined
    const observer = new ResizeObserver(() => setDocked(wideEnough(host)))
    observer.observe(host)
    return () => observer.disconnect()
  }, [host])
  return docked
}
