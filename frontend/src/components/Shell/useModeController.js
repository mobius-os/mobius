import { useCallback, useEffect, useLayoutEffect, useMemo, useReducer, useRef } from 'react'
import {
  initialModeState, modeReducer, completionContract, reconcileVisibleEvent,
  RECONCILE_SLACK_MS,
} from './modeMachine.js'
import { prefersReducedMotion } from './useLogoModeGesture.js'

// The React controller for the mode-transition descriptor (modeMachine.js). It
// owns the ONE reducer, funnels every mode-changing intent through it (INV 2),
// and drives completion by the transition EPOCH rather than a bare timer:
//
//   - a per-epoch layout effect captures the epoch in a closure and completes
//     ONLY that epoch via the animations' `finished` promises (INV 7): a delayed
//     animationend from a superseded epoch can never satisfy a newer one because
//     the completion carries the CAPTURED originating epoch;
//   - if no expected animation is running one frame after commit, it completes
//     synchronously (no-valid-target recovery);
//   - visibilitychange / pageshow reconciled from startedAt (INV 14): a beat whose
//     animation was throttled away while hidden is force-completed on return, with
//     NO scheduled correctness timer;
//   - a reduced-motion media subscription settles a live beat if the preference
//     flips mid-transition;
//   - a committedMode reconcile so an EXTERNAL durable-mode change can never let
//     the descriptor drift.
//
// The controller never persists anything and never touches the workspace tree —
// Shell builds the presentation plan and pairs each mode-committing intent with the
// matching workspace dispatch in the SAME handler so the pair batches as one
// transaction (INV 2/3).
export default function useModeController({
  committedMode, splitsEnabled = true, rootRef,
}) {
  const [state, dispatch] = useReducer(
    modeReducer, undefined, () => initialModeState(committedMode),
  )
  // The listener effects (visibility reconcile, reduced-motion, drag arm) read the
  // LIVE descriptor without re-subscribing every render. Updated in a LAYOUT effect
  // (not at render time) so an abandoned concurrent render can never publish a
  // descriptor the listeners then act on (W3).
  const stateRef = useRef(state)
  useLayoutEffect(() => { stateRef.current = state }, [state])

  // ── Reconcile with the external durable authority ──────────────────────────
  // workspace.viewMode is the persisted source of truth; the descriptor's
  // committedMode mirrors it. A controller-driven toggle updates both in one batch,
  // so they agree on the next render. Any OTHER path that moved viewMode (boot
  // hydration, a reload) shows up as a drift and is synced with NO beat. INV 3.
  useEffect(() => {
    if (committedMode !== stateRef.current.committedMode) {
      dispatch({ type: 'sync-committed', committedMode })
    }
  }, [committedMode])

  // ── Epoch-keyed completion (INV 7) ─────────────────────────────────────────
  // For each ANIMATED transition epoch we CAPTURE the epoch + its completion
  // contract from the COMMITTED render closure (this effect only runs for a
  // committed render, so `state` is the durable descriptor — never a render-written
  // ref that an abandoned render could have poisoned; W3). One frame after the beat
  // class commits, collect the plan's animations via getAnimations and await their
  // `finished` promises; when they settle — or if none are running (no valid
  // target) — dispatch complete{capturedEpoch}. The reducer's id guard then rejects
  // it unless it is still the live epoch, so a stale animation from epoch N can
  // NEVER clear epoch N+1.
  const transitionId = state.transition ? state.transition.id : null
  useEffect(() => {
    const contract = completionContract(state)
    if (!contract) return undefined
    const epoch = contract.id
    const names = contract.animationNames
    let cancelled = false
    let raf = 0
    const settle = () => { if (!cancelled) dispatch({ type: 'complete', id: epoch }) }
    const collect = () => {
      if (cancelled) return
      const root = rootRef?.current
      let anims = []
      if (root && typeof root.getAnimations === 'function') {
        try {
          anims = root.getAnimations({ subtree: true })
            .filter(a => names.has(a.animationName) && a.playState !== 'finished')
        } catch { anims = [] }
      }
      if (anims.length === 0) { settle(); return } // no valid target — recover
      // INV 7: complete on the Web Animations `finished` promises. allSettled so a
      // cancel (animationcancel → finished rejects) still settles this epoch.
      Promise.allSettled(anims.map(a => a.finished)).then(settle)
    }
    // ONE rAF: the class applies on the React commit; the CSS animations register
    // on the next style/layout, resolved before this frame's paint (exit-design v2).
    raf = requestAnimationFrame(collect)
    // W1: a bounded completion WATCHDOG (INV 7/14). The finished-promise path is the
    // primary mechanism, and the visibility reconcile handles a beat throttled away
    // while HIDDEN — but neither covers a beat whose `finished` promise never
    // resolves while the page stays VISIBLE (a stuck/cancelled animation, a target
    // that outlives its epoch). This timer force-completes the CAPTURED epoch at the
    // plan's totalMs + margin; the reducer's stale-epoch guard makes a late fire a
    // no-op if the beat already settled or was superseded. Armed only while an
    // animated beat is live and cleared on cleanup, this is the ONE correctness timer
    // in the mode system — nothing else schedules a bare timer.
    const watchdog = setTimeout(settle, contract.maxMs + RECONCILE_SLACK_MS)
    return () => {
      cancelled = true
      if (raf) cancelAnimationFrame(raf)
      clearTimeout(watchdog)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [transitionId, rootRef])

  // ── Visibility / pageshow reconcile (INV 14) ───────────────────────────────
  useEffect(() => {
    const reconcile = () => {
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return
      const ev = reconcileVisibleEvent(stateRef.current, now())
      if (ev) dispatch(ev)
    }
    document.addEventListener('visibilitychange', reconcile)
    window.addEventListener('pageshow', reconcile)
    return () => {
      document.removeEventListener('visibilitychange', reconcile)
      window.removeEventListener('pageshow', reconcile)
    }
  }, [])

  // ── Reduced-motion preference change mid-beat ──────────────────────────────
  // Enabling reduce during a live beat settles the captured epoch at once rather
  // than wait out an animation being removed underneath it.
  useEffect(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return undefined
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)')
    const onChange = () => {
      if (!mq.matches) return
      const t = stateRef.current.transition
      if (t && t.phase !== 'drag-preview') dispatch({ type: 'complete', id: t.id })
    }
    mq.addEventListener?.('change', onChange)
    return () => mq.removeEventListener?.('change', onChange)
  }, [])

  // ── Intent dispatchers (Shell calls these alongside its workspace dispatch) ─
  // Each is stable so it never churns the logo-gesture / drag-hook callbacks.

  // A user toggle or the last-tab-close auto-return. Shell builds the presentation
  // plan (deriveEnter/ExitPlan) and passes the HONEST cause ('hold'|'swipe'|
  // 'keyboard'|'auto'); a null presentation means an instant flip (reduced motion or
  // an empty tree — Shell passes none). `to` lets the auto-return pin the direction.
  // Returns the destination mode so Shell can drive the matching workspace side.
  const toggle = useCallback(({ cause, to, presentation } = {}) => {
    const from = stateRef.current.committedMode
    const dest = to || (from === 'single' ? 'panes' : 'single')
    // Reduced motion commits directly — never arm a descriptor (the plan is dropped
    // so planArms is false and the reducer flips instantly). One place owns this.
    const plan = prefersReducedMotion() ? null : presentation
    dispatch({ type: 'toggle', cause, to: dest, from, presentation: plan, now: now() })
    return dest
  }, [])

  // Undo restored the durable viewMode — routed here so the beat (re-entering
  // builder on a last-tab-close undo) is coordinated, not a silent bypass. INV 2.
  const undo = useCallback(({ restoredMode, presentation } = {}) => {
    const plan = prefersReducedMotion() ? null : presentation
    dispatch({ type: 'undo', restoredMode, presentation: plan, now: now() })
  }, [])

  // Drag arm / cancel / commit — the drag hook owns the id it passes back (INV 5).
  // The id is computed BEFORE dispatch (the reducer assigns state.nextId), so the
  // returned id is ATOMIC with the dispatch. Reading stateRef AFTER dispatch would
  // return the pre-dispatch epoch and a later cancel/blur would carry the wrong id.
  const dragArm = useCallback(() => {
    const s = stateRef.current
    if (s.committedMode !== 'single') {
      dispatch({ type: 'drag-arm', now: now() })
      return null
    }
    const id = s.nextId
    dispatch({ type: 'drag-arm', now: now() })
    return id
  }, [])
  const dragCancel = useCallback((id) => { dispatch({ type: 'drag-cancel', id }) }, [])
  const dragCommit = useCallback((id) => { dispatch({ type: 'drag-commit', id }) }, [])

  // Topology / geometry mutation invalidated the latched plan — cancel the beat
  // rather than let a transform retarget (INV 10). Wired to a workspace/geometry
  // watcher in Shell that compares the live exit signature to the latched one.
  const cancelBeat = useCallback(() => { dispatch({ type: 'cancel-beat' }) }, [])

  // One memoized API object so consumers that depend on the whole controller do not
  // churn every render; the callbacks above are already stable.
  return useMemo(() => ({
    state,
    toggle, undo, dragArm, dragCancel, dragCommit, cancelBeat,
  }), [state, toggle, undo, dragArm, dragCancel, dragCommit, cancelBeat])
}

function now() {
  return (typeof performance !== 'undefined' && performance.now)
    ? performance.now()
    : Date.now()
}
