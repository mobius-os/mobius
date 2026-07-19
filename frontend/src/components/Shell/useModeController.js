import { useCallback, useEffect, useReducer, useRef } from 'react'
import {
  initialModeState, modeReducer, completionContract, isCompletionSignal,
  reconcileVisibleEvent, needsSyncCompletion,
} from './modeMachine.js'
import { prefersReducedMotion } from './useLogoModeGesture.js'

// The React controller for the mode-transition descriptor (modeMachine.js). It
// owns the ONE reducer, funnels every mode-changing intent through it (INV 2),
// and drives completion by the transition EPOCH rather than a bare timer:
//
//   - animationend on the shell root, filtered by animation-name AND live epoch
//     (INV 12 + INV 15) — the primary, battle-tested completion path;
//   - a double-rAF getAnimations probe as the no-valid-target fallback (INV 13):
//     if the expected deal never actually runs while visible, complete at once;
//   - visibilitychange / pageshow reconciled from startedAt (INV 14): a beat
//     whose animationend was throttled away while hidden is force-completed on
//     return, with NO scheduled correctness timer;
//   - a committedMode reconcile so an EXTERNAL durable-mode change (hydration,
//     an undo that bypassed the controller) can never let the descriptor drift.
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
  // (boot hydration, a reload, an undo not routed through here) shows up as a
  // drift and is synced with NO beat (the accepted reload policy). INV 3.
  useEffect(() => {
    if (committedMode !== stateRef.current.committedMode) {
      dispatch({ type: 'sync-committed', committedMode })
    }
  }, [committedMode])

  // ── Keyed completion: animationend on the shell root (INV 12/15) ───────────
  useEffect(() => {
    const root = rootRef?.current
    if (!root) return undefined
    const onEnd = (e) => {
      const s = stateRef.current
      const t = s.transition
      if (!t) return
      // Filter by animation-name membership in the phase's set AND the live epoch
      // (isCompletionSignal enforces both). A sheet slide, a divider bar, or a
      // superseded epoch's late animationend can never end a mode beat.
      if (isCompletionSignal(s, t.id, e.animationName)) {
        dispatch({ type: 'complete', id: t.id })
      }
    }
    root.addEventListener('animationend', onEnd)
    root.addEventListener('animationcancel', onEnd)
    return () => {
      root.removeEventListener('animationend', onEnd)
      root.removeEventListener('animationcancel', onEnd)
    }
  }, [rootRef])

  // ── No-valid-target fallback (INV 13) ──────────────────────────────────────
  // If a live animated beat's expected deal never actually runs (no target
  // element, an animation the CSS did not apply), animationend will never fire.
  // Two frames after the transition commits we probe getAnimations; if nothing in
  // the expected set is running under the live epoch, complete synchronously. A
  // pure double-rAF probe, not a duration-sized timer. Reduced-motion beats are
  // already collapsed to instant flips in the reducer, so this only ever fires
  // for a genuinely absent target.
  const transitionId = state.transition ? state.transition.id : null
  useEffect(() => {
    const contract = completionContract(stateRef.current)
    if (!contract) return undefined
    let raf1 = 0
    let raf2 = 0
    const probe = () => {
      const root = rootRef?.current
      const s = stateRef.current
      const live = completionContract(s)
      if (!live || live.id !== contract.id) return // superseded — leave it
      let running = false
      if (root && typeof root.getAnimations === 'function') {
        try {
          running = root.getAnimations({ subtree: true }).some(
            a => live.animationNames.has(a.animationName) && a.playState !== 'finished',
          )
        } catch { running = false }
      }
      // No expected animation is running → the deal has no valid target; complete.
      if (!running) dispatch({ type: 'complete', id: contract.id })
    }
    raf1 = requestAnimationFrame(() => { raf2 = requestAnimationFrame(probe) })
    return () => {
      if (raf1) cancelAnimationFrame(raf1)
      if (raf2) cancelAnimationFrame(raf2)
    }
    // Re-probe whenever the live transition epoch changes.
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
  const dragArm = useCallback((focusedPaneId) => {
    dispatch({ type: 'drag-arm', focusedPaneId, now: now() })
    // The freshest epoch is the id the drag must carry to cancel/commit.
    return stateRef.current.transition ? stateRef.current.transition.id : null
  }, [])
  const dragCancel = useCallback((id) => { dispatch({ type: 'drag-cancel', id }) }, [])
  const dragCommit = useCallback((id) => { dispatch({ type: 'drag-commit', id }) }, [])

  // Topology mutation invalidated the latched exit roles — cancel the beat rather
  // than let animation ownership drift (INV 10).
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
