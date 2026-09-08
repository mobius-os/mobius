/** Reveals a panel's scrollbar during scrolling, with a short idle grace period. */
import { useEffect } from 'react'

// Long enough that momentum scrolling and a pause between wheel notches read
// as one movement; short enough that the thumb is gone before the next glance.
export const SCROLL_IDLE_HIDE_MS = 800

export default function useScrollActivity(panelRef, open) {
  useEffect(() => {
    const panel = panelRef.current
    if (!open || !panel) return
    let hideTimer
    const hide = () => { delete panel.dataset.scrolling }
    const onScroll = () => {
      panel.dataset.scrolling = ''
      clearTimeout(hideTimer)
      hideTimer = setTimeout(hide, SCROLL_IDLE_HIDE_MS)
    }
    hide()
    panel.addEventListener('scroll', onScroll, { passive: true })
    return () => {
      panel.removeEventListener('scroll', onScroll)
      clearTimeout(hideTimer)
      hide()
    }
  }, [panelRef, open])
}
