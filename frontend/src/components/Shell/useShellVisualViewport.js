/* Keep the full-screen shell inside the actually visible mobile viewport. */

import { useLayoutEffect } from 'react'
import {
  captureLayoutSpace,
  clientLengthToLayout,
} from '../../lib/layoutSpace.js'

// Ignore small browser-bar changes; a software keyboard consumes much more.
const MIN_KEYBOARD_INSET = 80

function clearShellFrame(root) {
  root.style.removeProperty('top')
  root.style.removeProperty('bottom')
  root.style.removeProperty('height')
  root.style.removeProperty('--chat-foot-bottom-inset')
}

/**
 * Fit the shell to a keyboard-shrunken visual viewport. Removing the previous
 * inline frame first makes the shell's own CSS layout the only baseline, so a
 * browser cannot poison the next opening with a stale window-height reading.
 */
export function fitShellToVisualViewport(root, viewport) {
  if (!root) return false
  clearShellFrame(root)

  const space = captureLayoutSpace(root)
  const visibleClientHeight = Number(viewport?.height)
  if (!(visibleClientHeight > 0)) return false
  const visibleHeight = clientLengthToLayout(visibleClientHeight, space)
  const layoutHeight = space.height
  const coveredHeight = layoutHeight - visibleHeight
  if (coveredHeight < clientLengthToLayout(MIN_KEYBOARD_INSET, space)) return false

  const visibleTop = Math.min(
    coveredHeight,
    Math.max(0, clientLengthToLayout(Number(viewport.offsetTop) || 0, space)),
  )
  root.style.setProperty('top', `${visibleTop}px`)
  root.style.setProperty('bottom', 'auto')
  root.style.setProperty('height', `${visibleHeight}px`)
  // The keyboard covers the Home indicator, so the chat composer rests directly
  // on it (UIKit's keyboardLayoutGuide behaves the same). Only this fitted,
  // keyboard-open frame zeroes the inset; otherwise the chat falls back to the
  // device's bottom safe area.
  root.style.setProperty('--chat-foot-bottom-inset', '0px')
  return true
}

export default function useShellVisualViewport(rootRef) {
  useLayoutEffect(() => {
    const viewport = window.visualViewport
    const root = rootRef.current
    if (!viewport || !root) return undefined

    let frameRequest = 0
    const apply = () => {
      frameRequest = 0
      fitShellToVisualViewport(root, viewport)
    }
    const applySoon = () => {
      if (!frameRequest) frameRequest = requestAnimationFrame(apply)
    }

    apply()
    viewport.addEventListener('resize', applySoon)
    viewport.addEventListener('scroll', applySoon)
    window.addEventListener('resize', applySoon)
    window.addEventListener('pageshow', applySoon)

    return () => {
      if (frameRequest) cancelAnimationFrame(frameRequest)
      viewport.removeEventListener('resize', applySoon)
      viewport.removeEventListener('scroll', applySoon)
      window.removeEventListener('resize', applySoon)
      window.removeEventListener('pageshow', applySoon)
      clearShellFrame(root)
    }
  }, [rootRef])
}
