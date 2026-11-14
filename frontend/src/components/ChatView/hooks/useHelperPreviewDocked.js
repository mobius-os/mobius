/* Dock a helper preview only when its owning chat has room for both columns. */
import { useLayoutEffect, useState } from 'react'

export default function useHelperPreviewDocked(host) {
  const [docked, setDocked] = useState(() => (host?.clientWidth || 0) >= 1000)
  useLayoutEffect(() => {
    if (!host || typeof ResizeObserver === 'undefined') return undefined
    const observer = new ResizeObserver(() => setDocked(host.clientWidth >= 1000))
    observer.observe(host)
    return () => observer.disconnect()
  }, [host])
  return docked
}
