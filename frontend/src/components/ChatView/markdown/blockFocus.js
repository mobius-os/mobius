/* Keyboard focus that a block's own control loses because the block changed
   under it (Send turning into Not now/Confirm, an action hidden while its work
   runs, Fix in chat settling) stays in the block instead of falling to the
   page: on the block's footer, so Tab reaches its next control, or on the
   block itself while it draws no footer. Nothing scrolls. */
import { useEffect, useLayoutEffect, useRef } from 'react'

const gone = node => !node?.isConnected || node.disabled === true

function place(node) {
  if (!node.hasAttribute('tabindex')) node.setAttribute('tabindex', '-1')
  node.focus({ preventScroll: true })
}

/** Watches one block section; call `settle()` after each render. */
export function createFocusKeeper(section, doc = section.ownerDocument) {
  // Focus is in the block, or was until the control holding it went away.
  let within = false
  // Focus waits on the block itself for a footer to come back.
  let owed = false
  const onIn = event => {
    within = true
    if (event.target !== section) owed = false
  }
  const onOut = event => {
    // Leaving the block gives up the claim; a control that vanished or turned
    // disabled under the reader does not. Chrome blurs a focused control while
    // removing it, before it is disconnected, so a blur to nothing is judged
    // once the change that caused it has finished.
    if (event.relatedTarget) {
      within = section.contains(event.relatedTarget)
      return
    }
    queueMicrotask(() => { within = gone(event.target) })
  }
  section.addEventListener('focusin', onIn)
  section.addEventListener('focusout', onOut)
  return {
    settle() {
      const active = doc.activeElement
      const footer = section.querySelector(':scope > footer')
      if (within && (!active || active === doc.body || (section.contains(active) && gone(active)))) {
        owed = !footer
        place(footer || section)
      } else if (owed && footer && active === section) {
        owed = false
        place(footer)
      }
    },
    dispose() {
      section.removeEventListener('focusin', onIn)
      section.removeEventListener('focusout', onOut)
    },
  }
}

/** Keeps a mounted block's lost focus in place after every render. */
export function useKeptFocus(section) {
  const keeper = useRef(null)
  useEffect(() => {
    if (!section) return undefined
    keeper.current = createFocusKeeper(section)
    return () => {
      keeper.current.dispose()
      keeper.current = null
    }
  }, [section])
  useLayoutEffect(() => { keeper.current?.settle() })
}
