import { useCallback, useEffect, useState } from 'react'

export const DESKTOP_SIDEBAR_QUERY = '(min-width: 1024px)'
export const DESKTOP_SIDEBAR_STORAGE_KEY = 'mobius:desktop-sidebar-open:v1'

export function readDesktopSidebarOpen(storage) {
  try {
    return storage?.getItem(DESKTOP_SIDEBAR_STORAGE_KEY) !== 'false'
  } catch {
    return true
  }
}

export function writeDesktopSidebarOpen(storage, open) {
  try {
    storage?.setItem(DESKTOP_SIDEBAR_STORAGE_KEY, String(Boolean(open)))
  } catch {
    // Private browsing and disabled storage keep the in-memory preference.
  }
}

function desktopQueryMatches() {
  return typeof window !== 'undefined'
    && Boolean(window.matchMedia?.(DESKTOP_SIDEBAR_QUERY).matches)
}

/**
 * Desktop navigation is ordinary layout state, deliberately separate from the
 * mobile drawer's history-backed virtual route in useNavigation.
 */
export default function useDesktopSidebar() {
  const [desktop, setDesktop] = useState(desktopQueryMatches)
  const [open, setOpenState] = useState(() => readDesktopSidebarOpen(
    typeof localStorage === 'undefined' ? null : localStorage,
  ))

  useEffect(() => {
    const query = window.matchMedia?.(DESKTOP_SIDEBAR_QUERY)
    if (!query) return undefined
    const update = () => setDesktop(query.matches)
    update()
    query.addEventListener('change', update)
    return () => query.removeEventListener('change', update)
  }, [])

  const setOpen = useCallback((nextOpen) => {
    const value = Boolean(nextOpen)
    setOpenState(value)
    writeDesktopSidebarOpen(
      typeof localStorage === 'undefined' ? null : localStorage,
      value,
    )
  }, [])

  return { desktop, open, setOpen }
}
