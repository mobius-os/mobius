import { useCallback, useEffect, useReducer, useRef } from 'react'
import {
  initialModeState, modeReducer, completionContract,
  reconcileVisibleEvent, needsSyncCompletion,
} from './modeMachine.js'
import { prefersReducedMotion } from './useLogoModeGesture.js'

// The React controller for the mode-transition descriptor (modeMachine.js). It
// owns the ONE reducer, funnels every mode-changing intent through it (INV 2),
// and drives completion by the transition EPOCH rather than a bare timer:
//
//   - a per-epoch layout effect captures the epoch in a closure and completes
//     ONLY that epoch via the animations' `finished` promises (INV 12 + INV 15):
//     a delayed animationend from a superseded epoch can never satisfy a newer
//     one because the completion carries the CAPTURED originating epoch, not the
//     current transition id inferred from an animation name;
//   - if no expected animation is running two frames after commit, it completes
//     synchronously (INV 13 no-valid-target);
//   - visibilitychange / pageshow reconciled from startedAt (INV 14): a beat whose
//     animation was throttled away while hidden is force-completed on return, with
//     NO scheduled correctness timer;
//   - a reduced-motion media subscription settles a live beat if the preference
//     flips mid-transition (INV 13);
//   - a committedMode reconcile so an EXTERNAL durable-mode change can never let
//     the descriptor drift.
//
// The controller never persists anything and never touches the workspace tree —
// Shell pairs each mode-committing intent with the matching workspace dispatch in
// the SAME event handler so the pair batches as one transaction (INV 7).
export default function useModeController({
  committedMode, splitsEnabled = true, rootRef,
}) {
  const [state, dispatch] = useReducer(
    modeReducer, undefined, () => initialModeState(committedMode),
  )
  // The listeners read the LIVE descriptor without re-subscribing every render.
  const stateRef = useRef(state)
  stateRef.current = state

  // ── Reconcile with the external durable authority ──────────────────────────
  // workspace.viewMode is the persisted source of truth; the descriptor's
  // committedMode mirrors it. A controller-driven toggle updates both in one
  // batch, so they agree on the next render. Any OTHER path that moved viewMode
  // (boot hydration, a reload) shows up as a drift and is synced with NO beat (the
  // accepted reload policy). Mode-changing Undo/auto-return are routed through the
  // controller at their dispatch sites (INV 2), so this is the hydration net, not
  // the beat path. INV 3.
  useEffect(() => {
    if (committedMode !== stateRef.current.committedMode) {
      dispatch({ type: 'sync-committed', committedMode })
    }
  }, [committedMode])

  // ── Epoch-keyed completion (INV 12/15) ─────────────────────────────────────
  // For each ANIMATED transition epoch we CAPTURE the epoch in this closure and
  // complete ONLY that epoch. Two frames after the beat class commits (so the CSS
  // animations are registered), collect the expected animations via getAnimations
  // and await their `finished` promises; when they settle — or if none are running
  // (INV 13, no valid target) — dispatch complete{capturedEpoch}. The reducer's id
  // guard then rejects it unless it is still the live epoch, so a stale animation
  // from epoch N can NEVER clear epoch N+1 (the epoch-inference flaw the review
  // flagged: a delayed same-named animation must not satisfy a newer beat).
  const transitionId = state.transition ? state.transition.id : null
  useEffect(() => {
    const contract = completionContract(stateRef.current)
    if (!contract) return undefined
    const epoch = contract.id
    const names = contract.animationNames
    let cancelled = false
    let raf1 = 0
    let raf2 = 0
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
      if (anims.length === 0) { settle(); return } // INV 13: no valid target
      // INV 12: complete on the Web Animations `finished` promises. allSettled so
      // a cancel (animationcancel → finished rejects) still settles this epoch.
      Promise.allSettled(anims.map(a => a.finished)).then(settle)
    }
    // The class applies on the React commit; the CSS animation registers on the
    // next style/layout, so probe on the SECOND frame.
    raf1 = requestAnimationFrame(() => { raf2 = requestAnimationFrame(collect) })
    return () => {
      cancelled = true
      if (raf1) cancelAnimationFrame(raf1)
      if (raf2) cancelAnimationFrame(raf2)
    }
  }, [transitionId, rootRef])

  // Reduced-motion / structurally-instant beats never reach the reducer as a live
  // transition (they collapse to transition:null there), so this is a defensive
  // parity check: if one somehow armed with animated:false, settle it at once.
  useEffect(() => {
    if (needsSyncCompletion(stateRef.current)) {
      const t = stateRef.current.transition
      if (t) dispatch({ type: 'complete', id: t.id })
    }
  }, [transitionId])

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

  // ── Reduced-motion preference change mid-beat (INV 13) ─────────────────────
  // Enabling reduce during a live beat must settle the descriptor at once rather
  // than wait out an animation that is being removed underneath it.
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

  // A user toggle (hold / swipe / keyboard). Shell supplies the tree facts the
  // beat needs; the controller fills reduced-motion + the clock. Returns the
  // destination mode so Shell can drive the matching workspace + Settings side.
  const toggle = useCallback(({ focusedPaneId, leavingPaneIds, multiPane } = {}) => {
    const from = stateRef.current.committedMode
    const to = from === 'single' ? 'panes' : 'single'
    dispatch({
      type: 'toggle', cause: 'hold', to, from,
      focusedPaneId, leavingPaneIds, multiPane,
      reducedMotion: prefersReducedMotion(), now: now(),
    })
    return to
  }, [])

  // A forced-direction change (last-tab-close auto-return, single-leaf drop flip).
  const autoFlip = useCallback(({ to, focusedPaneId, leavingPaneIds, multiPane } = {}) => {
    dispatch({
      type: 'auto-flip', to,
      focusedPaneId, leavingPaneIds, multiPane,
      reducedMotion: prefersReducedMotion(), now: now(),
    })
  }, [])

  // Undo restored the durable viewMode — routed here so the beat (re-entering
  // builder on a last-tab-close undo) is coordinated, not a silent bypass. INV 2.
  const undo = useCallback(({ restoredMode, focusedPaneId, leavingPaneIds, multiPane } = {}) => {
    dispatch({
      type: 'undo', restoredMode,
      focusedPaneId, leavingPaneIds, multiPane,
      reducedMotion: prefersReducedMotion(), now: now(),
    })
  }, [])

  // Drag arm / cancel / commit — the drag hook owns the id it passes back (INV 5).
  // The id is computed BEFORE dispatch (the reducer assigns state.nextId), so the
  // returned id is ATOMIC with the dispatch. Reading stateRef AFTER dispatch would
  // return the pre-dispatch (stale) epoch — a useReducer dispatch is async — and a
  // later cancel/blur would then carry the wrong id and never clear the live
  // preview, leaving the workspace permanently tiled (the wedge, reincarnated).
  const dragArm = useCallback((focusedPaneId) => {
    const s = stateRef.current
    // Only a single-mode drag creates a preview (the reducer no-ops otherwise).
    if (s.committedMode !== 'single') {
      dispatch({ type: 'drag-arm', focusedPaneId, now: now() })
      return null
    }
    const id = s.nextId
    dispatch({ type: 'drag-arm', focusedPaneId, now: now() })
    return id
  }, [])
  const dragCancel = useCallback((id) => { dispatch({ type: 'drag-cancel', id }) }, [])
  const dragCommit = useCallback((id) => { dispatch({ type: 'drag-commit', id }) }, [])

  // Topology mutation invalidated the latched exit roles — cancel the beat rather
  // than let animation ownership drift (INV 10). Wired to a workspace-tree watcher
  // in Shell so a pane close/move/placement/undo during an exit settles the class.
  const cancelBeat = useCallback(() => { dispatch({ type: 'cancel-beat' }) }, [])

  return {
    state,
    toggle, autoFlip, undo, dragArm, dragCancel, dragCommit, cancelBeat,
  }
}

function now() {
  return (typeof performance !== 'undefined' && performance.now)
    ? performance.now()
    : Date.now()
}
