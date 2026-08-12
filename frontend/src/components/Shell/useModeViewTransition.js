import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { flushSync } from 'react-dom'
import { prefersReducedMotion } from './useLogoModeGesture.js'

// Mode changes are scene changes, not live-layout animations. The browser captures
// the settled Standard and Builder worlds, then this hook moves only the captured
// pane pixels on one document timeline. Heavy retained chats can wake, measure, and
// raster behind the old snapshot without consuming the visible motion clock.

export const MODE_WORKSPACE_TRANSITION_NAME = 'mode-workspace'
export const MODE_NAVIGATION_BAR_TRANSITION_NAME = 'mode-navigation-bar'
export const MODE_NAVIGATION_DRAWER_TRANSITION_NAME = 'mode-navigation-drawer'

function ident(value) {
  const token = String(value ?? 'surface').replace(/[^a-zA-Z0-9_-]/g, '-')
  return token || 'surface'
}

export function modeViewTransitionName(kind, ...parts) {
  return `mode-${ident(kind)}-${parts.map(ident).join('-')}`
}

export function modeViewTransitionStyle(kind, paneId, identity = kind) {
  // Keep view-transition-name itself off resting surfaces. It is enabled from
  // this inert custom ident only while the root transaction attribute is present,
  // so normal scrolling carries no extra capture/paint-containment contract.
  return { '--mode-vt-name': modeViewTransitionName(kind, paneId, identity) }
}

function participantSnapshots(root, offsets) {
  if (!root || !offsets) return []
  const out = []
  for (const element of root.querySelectorAll('[data-mode-pane-vt]')) {
    const paneId = element.getAttribute('data-mode-pane-vt')
    const offset = offsets[paneId]
    if (!offset) continue
    const name = getComputedStyle(element).viewTransitionName
    if (!name || name === 'none') continue
    const softEntry = element.hasAttribute('data-mode-strip-soft-entry')
    out.push({
      name,
      offset,
      // Tiled strips stay attached to pane travel; only the single strip opts in.
      softEntry,
      // Capture rendered geometry rather than duplicating paneModel.STRIP_H in
      // the animation owner. The travel stays correct if the shared token changes.
      softEntryDistance: softEntry
        ? Math.max(1, Math.ceil(element.getBoundingClientRect().height))
        : null,
    })
  }
  return out
}

export function softStripKeyframes(direction, distancePx) {
  const distance = Math.max(1, Math.ceil(Number(distancePx) || 0))
  const above = `translate3d(0, -${distance}px, 0)`
  const settled = 'translate3d(0, 0, 0)'
  return direction === 'exit'
    ? [
        { opacity: 1, transform: settled },
        { opacity: 1, transform: above },
      ]
    : [
        { opacity: 1, transform: above },
        { opacity: 1, transform: settled },
      ]
}

function stationarySnapshotNames(root) {
  if (!root) return []
  const names = []
  for (const element of root.querySelectorAll('.shell__bar, .drawer--open')) {
    const name = getComputedStyle(element).viewTransitionName
    if (name && name !== 'none') names.push(name)
  }
  return names
}

function animatePseudo(root, pseudoElement, keyframes, timing, startTime) {
  const animation = root.animate(keyframes, { ...timing, pseudoElement })
  // Animations created in the same microtask are already extremely close. Pinning
  // them to one timeline instant removes even that construction-order difference.
  if (Number.isFinite(startTime)) {
    try { animation.startTime = startTime } catch { /* timeline pin unsupported */ }
  }
  return animation
}

export default function useModeViewTransition({ rootRef, durationMs }) {
  const [active, setActive] = useState(null)
  const liveRef = useRef(null)
  const nextIdRef = useRef(1)

  const settle = useCallback((id) => {
    if (liveRef.current?.descriptor.id !== id) return
    for (const animation of liveRef.current.animations) {
      try { animation.cancel() } catch { /* pseudo already gone */ }
    }
    liveRef.current = null
    delete document.documentElement.dataset.modeViewTransition
    setActive(null)
  }, [])

  const run = useCallback(({ direction, to, cause = 'toggle', plan, update }) => {
    const phase = direction === 'enter' ? 'entering' : 'exiting'
    const id = nextIdRef.current++
    const descriptor = {
      id, phase, direction, to, cause,
      totalMs: durationMs,
    }
    const supported = typeof document !== 'undefined'
      && typeof document.startViewTransition === 'function'
      && !prefersReducedMotion()
      && plan?.offsets

    if (!supported) {
      flushSync(update)
      return { animated: false, totalMs: 0, transitionId: null, to }
    }

    // Mode input is shielded while active, but a keyboard event can still reach the
    // header. Finish the old visual transaction before accepting a new scene.
    if (liveRef.current) {
      for (const animation of liveRef.current.animations) {
        try { animation.cancel() } catch { /* pseudo already gone */ }
      }
      try { liveRef.current.transition.skipTransition() } catch { /* already ending */ }
    }

    const html = document.documentElement
    const shell = rootRef?.current
    // Publish the descriptor before startViewTransition returns control to the
    // caller. A completed logo hold uses the run receipt to hand its compression
    // to this exact descriptor; waiting for the browser's asynchronous update
    // callback left one render with no active beat, so the gesture correctly
    // cleared its ownership latch before the scene had even begun.
    flushSync(() => setActive(descriptor))
    html.dataset.modeViewTransition = direction
    // Capture names only exist while the root transaction attribute is present.
    // Enable it before reading the departing scene; entry reads after the final
    // Builder world has committed inside the update callback below.
    let snapshots = direction === 'exit'
      ? participantSnapshots(shell, plan.offsets)
      : []
    const stationaryNames = stationarySnapshotNames(shell)

    let transition
    try {
      transition = document.startViewTransition(() => {
        flushSync(() => {
          update()
        })
        if (direction === 'enter') snapshots = participantSnapshots(shell, plan.offsets)
      })
    } catch {
      delete html.dataset.modeViewTransition
      flushSync(() => {
        setActive(null)
        update()
      })
      return { animated: false, totalMs: 0, transitionId: null, to }
    }

    liveRef.current = { descriptor, transition, animations: [] }
    transition.ready.then(() => {
      if (liveRef.current?.descriptor.id !== id) return
      if (snapshots.length === 0) {
        transition.skipTransition()
        return
      }
      const animations = liveRef.current.animations
      const timing = { duration: durationMs, easing: 'linear', fill: 'both' }
      const startTime = document.timeline?.currentTime
      const workspaceSide = direction === 'enter' ? 'old' : 'new'
      animations.push(animatePseudo(
        html,
        `::view-transition-${workspaceSide}(${MODE_WORKSPACE_TRANSITION_NAME})`,
        [{ opacity: 1 }, { opacity: 1 }],
        timing,
        startTime,
      ))
      // Navigation remains a foreground layer in both worlds. Capturing its final
      // pixels separately preserves the drawer/rail clipping boundary while panes
      // travel beneath it, without involving live navigation DOM in the motion.
      for (const name of stationaryNames) {
        animations.push(animatePseudo(
          html,
          `::view-transition-new(${name})`,
          [{ opacity: 1 }, { opacity: 1 }],
          timing,
          startTime,
        ))
      }
      const paneSide = direction === 'enter' ? 'new' : 'old'
      for (const { name, offset, softEntry, softEntryDistance } of snapshots) {
        const away = `translate3d(${offset.x}px, ${offset.y}px, 0)`
        let keyframes
        if (softEntry) {
          keyframes = softStripKeyframes(direction, softEntryDistance)
        } else if (direction === 'enter') {
          keyframes = [
            { opacity: 1, transform: away },
            { opacity: 1, transform: 'translate3d(0, 0, 0)' },
          ]
        } else {
          keyframes = [
            { opacity: 1, transform: 'translate3d(0, 0, 0)' },
            { opacity: 1, transform: away },
          ]
        }
        animations.push(animatePseudo(
          html,
          `::view-transition-${paneSide}(${name})`,
          keyframes,
          timing,
          startTime,
        ))
      }
    }).catch(() => {
      try { transition.skipTransition() } catch { /* already skipped */ }
    })
    transition.finished.then(
      () => settle(id),
      () => settle(id),
    )

    return { animated: true, totalMs: durationMs, transitionId: id, to }
  }, [durationMs, rootRef, settle])

  useEffect(() => () => {
    const live = liveRef.current
    liveRef.current = null
    if (typeof document !== 'undefined') {
      delete document.documentElement.dataset.modeViewTransition
    }
    for (const animation of live?.animations || []) {
      try { animation.cancel() } catch { /* pseudo already gone */ }
    }
    try { live?.transition.skipTransition() } catch { /* already finished */ }
  }, [])

  return useMemo(() => ({ active, run }), [active, run])
}
