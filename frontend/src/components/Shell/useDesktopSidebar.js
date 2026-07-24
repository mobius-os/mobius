import { useCallback, useEffect, useState } from 'react'

export const DESKTOP_SIDEBAR_QUERY = '(min-width: 1024px)'
export const DESKTOP_SIDEBAR_STORAGE_KEY = 'mobius:desktop-sidebar-open:v1'
export const DESKTOP_SIDEBAR_WIDTH_STORAGE_KEY = 'mobius:desktop-sidebar-width:v1'
export const DESKTOP_SIDEBAR_DEFAULT_WIDTH = 320
export const DESKTOP_SIDEBAR_MIN_WIDTH = 240
export const DESKTOP_SIDEBAR_MAX_WIDTH = 560

export function clampDesktopSidebarWidth(width) {
  const numericWidth = Number(width)
  if (!Number.isFinite(numericWidth)) return DESKTOP_SIDEBAR_DEFAULT_WIDTH
  return Math.min(
    DESKTOP_SIDEBAR_MAX_WIDTH,
    Math.max(DESKTOP_SIDEBAR_MIN_WIDTH, Math.round(numericWidth)),
  )
}

/**
 * Project the content width for one desktop-sidebar open/close toggle.
 *
 * Shell uses this before dispatching the sidebar preference update so
 * React receives the CSS reservation and the pane geometry in one render. The
 * current content measurement is live DOM truth; adding back the current
 * reservation recovers the shell width without duplicating viewport math.
 */
export function desktopContentWidthAfterSidebarToggle(currentContentWidth, {
  currentReserved = false,
  nextReserved = false,
  sidebarWidth = DESKTOP_SIDEBAR_DEFAULT_WIDTH,
} = {}) {
  const measured = Number(currentContentWidth)
  const contentWidth = Number.isFinite(measured) ? Math.max(0, measured) : 0
  const width = clampDesktopSidebarWidth(sidebarWidth)
  const shellWidth = contentWidth
    + (currentReserved ? width : 0)
  const projected = shellWidth
    - (nextReserved ? width : 0)
  return Math.max(0, Math.round(projected))
}

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

export function readDesktopSidebarWidth(storage) {
  try {
    const stored = storage?.getItem(DESKTOP_SIDEBAR_WIDTH_STORAGE_KEY)
    return stored == null || stored === ''
      ? DESKTOP_SIDEBAR_DEFAULT_WIDTH
      : clampDesktopSidebarWidth(stored)
  } catch {
    return DESKTOP_SIDEBAR_DEFAULT_WIDTH
  }
}

export function writeDesktopSidebarWidth(storage, width) {
  try {
    storage?.setItem(
      DESKTOP_SIDEBAR_WIDTH_STORAGE_KEY,
      String(clampDesktopSidebarWidth(width)),
    )
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
  const [width, setWidthState] = useState(() => readDesktopSidebarWidth(
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

  const setWidth = useCallback((nextWidth) => {
    const value = clampDesktopSidebarWidth(nextWidth)
    setWidthState(value)
    writeDesktopSidebarWidth(
      typeof localStorage === 'undefined' ? null : localStorage,
      value,
    )
  }, [])

  return { desktop, open, setOpen, width, setWidth }
}
