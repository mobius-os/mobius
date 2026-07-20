import { useEffect, useState } from 'react'
import { haloFrame } from './logoHoldMachine.js'
import { prefersReducedMotion } from './useLogoModeGesture.js'

// The LIVING HALO — the logo's "lit soul" in builder mode. A radial-gradient
// element behind the mark whose scale/offset/opacity drift on two summed sines at
// irrational frequencies (never a visible loop). One single rAF, ONE reused frame
// object → zero per-frame allocation. Pauses on a hidden tab and is killed
// instantly when builder mode deactivates (the effect cleanup). Under reduced
// motion it settles to a static low halo with NO rAF at all. The animated values
// are written directly to the halo element so each frame invalidates only that
// leaf, rather than a brand ancestor and all of its descendants. Per-theme base
// alpha rides the --halo-alpha token.
function clearHaloStyles(el) {
  el.style.removeProperty('translate')
  el.style.removeProperty('scale')
  el.style.removeProperty('--halo-opacity')
}

export default function useLivingHalo({ haloRef, active }) {
  // Track the reduced-motion preference REACTIVELY (finding 13): sampling it once
  // at effect-run left the rAF running when the owner enabled reduce mid-session.
  // Making it state that the effect depends on re-runs the effect on a preference
  // flip, which cancels the loop and settles the static halo (or restarts it).
  const [reduced, setReduced] = useState(prefersReducedMotion)
  useEffect(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return undefined
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)')
    const onChange = () => setReduced(mq.matches)
    onChange()
    mq.addEventListener?.('change', onChange)
    return () => mq.removeEventListener?.('change', onChange)
  }, [])
  useEffect(() => {
    const el = haloRef?.current
    if (!el || !active) return undefined
    if (reduced) {
      el.style.translate = '0px 0px'
      el.style.scale = '1'
      el.style.setProperty('--halo-opacity', '0.8')
      return () => clearHaloStyles(el)
    }
    let raf = 0
    const frame = {} // reused every tick — no allocation in the loop
    const loop = () => {
      haloFrame(performance.now(), frame)
      el.style.translate = `${frame.x}px ${frame.y}px`
      el.style.scale = String(frame.scale)
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
      clearHaloStyles(el)
    }
  }, [haloRef, active, reduced])
}
