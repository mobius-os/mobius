import { useEffect } from 'react'
import { haloFrame } from './logoHoldMachine.js'
import { prefersReducedMotion } from './useLogoModeGesture.js'

// The LIVING HALO — the logo's "lit soul" in builder mode. A radial-gradient
// element behind the mark whose scale/offset/opacity drift on two summed sines at
// irrational frequencies (never a visible loop). One single rAF, ONE reused frame
// object → zero per-frame allocation. Pauses on a hidden tab and is killed
// instantly when builder mode deactivates (the effect cleanup). Under reduced
// motion it settles to a static low halo with NO rAF at all. Writes the drift as
// CSS vars on the brand element; the CSS (Shell.css) composes them. Per-theme base
// alpha rides the --halo-alpha token.
export default function useLivingHalo({ brandRef, active }) {
  useEffect(() => {
    const el = brandRef?.current
    if (!el || !active) return undefined
    if (prefersReducedMotion()) {
      el.style.setProperty('--halo-scale', '1')
      el.style.setProperty('--halo-x', '0px')
      el.style.setProperty('--halo-y', '0px')
      el.style.setProperty('--halo-opacity', '0.8')
      return undefined
    }
    let raf = 0
    const frame = {} // reused every tick — no allocation in the loop
    const loop = () => {
      haloFrame(performance.now(), frame)
      el.style.setProperty('--halo-scale', String(frame.scale))
      el.style.setProperty('--halo-x', `${frame.x}px`)
      el.style.setProperty('--halo-y', `${frame.y}px`)
      el.style.setProperty('--halo-opacity', String(frame.opacity))
      raf = requestAnimationFrame(loop)
    }
    const onVisibility = () => {
      if (document.visibilityState === 'hidden') {
        if (raf) { cancelAnimationFrame(raf); raf = 0 }
      } else if (!raf) {
        raf = requestAnimationFrame(loop)
      }
    }
    raf = requestAnimationFrame(loop)
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      if (raf) cancelAnimationFrame(raf)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [brandRef, active])
}
