// The mode-transition machine — the ONE descriptor that replaces the old
// three-boolean + two-timer + shared-render-override tangle (builderEntering,
// builderExiting, dragPreviewBuilder, builderEnterTimerRef, builderExitTimerRef
// and the effectiveViewMode expression scattered across Shell.jsx).
//
// Codex's adversarial review (codex-mode-machinery-review.md §3) found that the
// old shape "cannot be made sequence-proof with local guards": rapid re-entry,
// enter-during-exit, drag-arm-during-beat, undo-during-beat, and Settings
// conversion each stranded an independent flag/timer/class. The prescribed
// replacement is a single exclusive transition descriptor from which EVERYTHING
// derives, with completion keyed to the transition epoch rather than a bare
// timer. This module is that descriptor, its pure reducer, and every derivation —
// all DOM-free and timer-free so the whole contract is unit-testable (the
// mandatory matrix in review §4 runs against this file).
//
// State shape (review §3):
//
//   { committedMode: 'single' | 'panes',
//     transition: null | {
//       id,               // monotonic epoch — the completion key (INV 15)
//       phase,            // 'entering' | 'exiting' | 'drag-preview'
//       from, to,         // 'single' | 'panes'
//       cause,            // 'hold'|'swipe'|'keyboard'|'drag'|'undo'|'auto-flip'|'boot'
//       startedAt,        // ms — the reconcile clock (INV 14)
//       focusedPaneId,    // latched settling pane (INV 9)
//       leavingPaneIds,   // latched leaving panes (INV 9)
//       animated,         // false ⇒ complete synchronously (INV 13)
//     } }
//
// The 16 required invariants are cited inline as `INV N` at their enforcement
// point so a reviewer can grep any invariant number to its code.
//
//  1. At most one transition exists.
//  2. Every mode-changing path goes through this one reducer.
//  3. New input supersedes the prior epoch synchronously.
//  4. effectiveViewMode, geometry, root class, logo/halo, Settings presentation,
//     power chrome, aria, interactivity all derive from this one descriptor.
//  5. Drag arm is phase 'drag-preview'; cancel and commit carry its id.
//  6. Settings has one canonical location; tab vs takeover is presentation.
//  7. Mode + Settings + drop + undo snapshot is one transaction (the controller
//     batches this reducer's event with the workspace dispatch).
//  8. Explicit later mode intent rebases a mode-coupled undo to tree-only.
//  9. Exit latches focusedPaneId + leavingPaneIds; leaving surfaces are inert.
// 10. Topology mutation during a beat cancels/rebases the descriptor.
// 11. No one-shot animation on a permanent class (enforced in workspace.css).
// 12. Completion is a filtered, keyed animationend / finished promise.
// 13. Reduced motion or a missing animation target completes synchronously.
// 14. visibilitychange / pageshow reconciles from startedAt; no bare timer.
// 15. Stale completion from id=N cannot clear id=N+1.
// 16. The splits kill switch clamps presentation to single before derivation.

// Durations. Kept here (not in CSS alone) so the reconcile clock (INV 14) and
// the missing-target fallback (INV 13) can reason about the maximum a beat can
// legitimately run. These MUST stay in lockstep with workspace.css: the entry
// deal (shell-pane-deal / shell-strip-deal-in / shell-pane-settle) and the exit
// deal-out (shell-pane-deal-out).
export const MODE_ENTER_MS = 400 // longest entry animation (multi-pane deal)
export const MODE_EXIT_MS = 240 // reverse deal-out (the "Zippo asymmetry")
// The slack a visibility-return reconcile allows past the nominal duration
// before it force-completes a beat whose animationend never arrived (hidden tab,
// throttled rAF). Not a correctness timer — nothing fires it on its own; it is a
// pure comparison against startedAt at the reconcile boundary (INV 14).
export const RECONCILE_SLACK_MS = 60

// The set of CSS animation-names a given phase's completion listens for (INV
// 12). The controller filters `animationend` by BOTH the epoch (INV 15) and
// membership in this set, so an unrelated animation (a sheet slide, a divider
// bar) can never complete a mode beat, and a superseded epoch's late animationend
// is ignored.
const ENTER_ANIMATIONS = new Set([
  'shell-pane-deal', // multi-pane entry: each pane deals in
  'shell-strip-deal-in', // single-leaf entry: the strip deals down
  'shell-pane-settle', // single-leaf entry: the pane lift-settles
])
const EXIT_ANIMATIONS = new Set([
  'shell-pane-deal-out', // the leaving pane deals back out to the right
])

export function initialModeState(committedMode = 'panes') {
  return {
    committedMode: committedMode === 'single' ? 'single' : 'panes',
    transition: null,
    // Monotonic epoch source. Every transition takes `nextId` and increments it,
    // so no two transitions in a session ever share an id — the guarantee INV 15
    // (a stale completion from id=N cannot clear id=N+1) rests on.
    nextId: 1,
  }
}

// Whether a direction change should carry an animated beat at all. Entering
// always has a moment; exiting only earns the reverse deal when the workspace is
// tiled (a single leaf has no sibling to deal out). Reduced motion drops both
// (the beat becomes a synchronous flip). INV 13.
function beatIsAnimated({ to, multiPane, reducedMotion }) {
  if (reducedMotion) return false
  if (to === 'single') return !!multiPane // exit: only a tiled workspace deals out
  return true // enter: always a moment
}

// Build a fresh transition descriptor for a committed direction change, taking
// (and consuming) the monotonic epoch. Returns { transition, nextId }.
function makeTransition(state, {
  from, to, cause, focusedPaneId, leavingPaneIds, animated, now,
}) {
  const id = state.nextId
  return {
    nextId: id + 1,
    transition: {
      id,
      phase: to === 'panes' ? 'entering' : 'exiting',
      from,
      to,
      cause: cause || 'toggle',
      startedAt: Number.isFinite(now) ? now : 0,
      // Latched exit roles (INV 9). Only meaningful for an exit beat; harmless
      // (ignored) for an entry. Copied so a later topology change to the live
      // arrays cannot mutate the latched set.
      focusedPaneId: focusedPaneId ?? null,
      leavingPaneIds: Array.isArray(leavingPaneIds) ? [...leavingPaneIds] : [],
      animated,
    },
  }
}

// The pure reducer. Every mode-changing path in the app funnels here (INV 2);
// the controller (useModeController) is the only caller and pairs each
// mode-committing event with its workspace dispatch so the pair is one
// transaction (INV 7).
export function modeReducer(state, event) {
  switch (event.type) {
    // A user toggle (hold / swipe / keyboard) or a forced-direction auto-flip.
    // Direction is decided by the CURRENT committedMode unless `to` is given
    // (auto-flip / undo pass it explicitly). The committed mode flips
    // SYNCHRONOUSLY here; the render holds the outgoing world during an exit beat
    // via effectiveViewMode (below), never by deferring the durable flip — so the
    // reducer and the render can never disagree for longer than the beat (the P0
    // the review opened with). INV 2, INV 3.
    case 'toggle':
    case 'auto-flip': {
      const from = state.committedMode
      const to = event.to || (from === 'single' ? 'panes' : 'single')
      // A toggle to the mode already committed with no live beat is a genuine
      // no-op; return the same reference so React can bail.
      if (to === from && !state.transition) return state
      const animated = beatIsAnimated({
        to, multiPane: event.multiPane, reducedMotion: event.reducedMotion,
      })
      if (!animated) {
        // Instant flip — no beat to strand (reduced motion, or a single-leaf
        // exit). Any prior transition is superseded and dropped (INV 3).
        return { ...state, committedMode: to, transition: null }
      }
      const { transition, nextId } = makeTransition(state, {
        from,
        to,
        cause: event.cause || (event.type === 'auto-flip' ? 'auto-flip' : 'toggle'),
        focusedPaneId: event.focusedPaneId,
        leavingPaneIds: event.leavingPaneIds,
        animated: true,
        now: event.now,
      })
      // INV 3: a new toggle REPLACES whatever transition was live (enter→exit,
      // exit→enter, repeated enter, …) with a new epoch, synchronously. The old
      // epoch's pending completion is now stale and cannot touch this one (INV 15).
      return { committedMode: to, transition, nextId }
    }

    // A single-mode drag previews the builder world without committing the durable
    // mode (design: "dragging is building" — the preview, not the commit). INV 5.
    // Only meaningful from single: in builder the workspace is already tiled, so a
    // drag is ordinary reorganization and needs no preview. Supersedes any live
    // beat (arm-during-entry / arm-during-exit in the matrix). INV 3.
    case 'drag-arm': {
      if (state.committedMode !== 'single') {
        // In builder, arming does not change the descriptor. Drop any stale
        // transition defensively (there should be none) but keep committedMode.
        return state.transition ? { ...state, transition: null } : state
      }
      const id = state.nextId
      return {
        committedMode: 'single',
        transition: {
          id,
          phase: 'drag-preview',
          from: 'single',
          to: 'single', // a preview commits nothing until drag-commit
          cause: 'drag',
          startedAt: Number.isFinite(event.now) ? event.now : 0,
          focusedPaneId: event.focusedPaneId ?? null,
          leavingPaneIds: [],
          animated: false, // the preview has no entry/exit deal of its own
        },
        nextId: id + 1,
      }
    }

    // Cancel/Escape/blur/lost-capture end of a drag preview. INV 5, INV 15: only
    // the matching live drag-preview epoch is cleared — a stale cancel from a
    // superseded drag is ignored.
    case 'drag-cancel': {
      const t = state.transition
      if (!t || t.phase !== 'drag-preview' || t.id !== event.id) return state
      return { ...state, transition: null }
    }

    // A drop committed. The workspace reducer performs the placement + the
    // 'panes' flip (OPEN_TAB_AT flipViewMode) in the SAME batch (INV 7); here we
    // reflect committedMode='panes' and drop the preview. Only the matching live
    // drag-preview epoch commits (INV 5, INV 15). A rejected / no-op drop must NOT
    // reach here (the controller only dispatches drag-commit for a real placement),
    // so this never fabricates a mode change from a no-op (the P1 review finding).
    case 'drag-commit': {
      const t = state.transition
      if (!t || t.phase !== 'drag-preview' || t.id !== event.id) return state
      return { ...state, committedMode: 'panes', transition: null }
    }

    // Undo restored the durable viewMode. Undo is a first-class controller event
    // (INV 2) rather than a silent reducer bypass. If the restored mode differs
    // from the committed one it is a real direction change and earns a beat (an
    // undo of a last-tab-close auto-return re-enters builder with the entry deal);
    // an ordinary tree undo carries the current mode forward, so restoredMode ===
    // committedMode and this is transition:null.
    case 'undo': {
      const to = event.restoredMode === 'single' ? 'single' : 'panes'
      const from = state.committedMode
      if (to === from) {
        return state.transition ? { ...state, transition: null } : state
      }
      const animated = beatIsAnimated({
        to, multiPane: event.multiPane, reducedMotion: event.reducedMotion,
      })
      if (!animated) return { ...state, committedMode: to, transition: null }
      const { transition, nextId } = makeTransition(state, {
        from,
        to,
        cause: 'undo',
        focusedPaneId: event.focusedPaneId,
        leavingPaneIds: event.leavingPaneIds,
        animated: true,
        now: event.now,
      })
      return { committedMode: to, transition, nextId }
    }

    // Reconcile committedMode with an EXTERNAL durable-mode change — hydration on
    // boot/reload, or any viewMode change that did not originate here (a defensive
    // sync so the descriptor can never drift from the persisted authority). A
    // reload starts directly in the durable mode with NO beat (the review's
    // accepted reload policy), so this always clears any transition. Same
    // reference when already in sync.
    case 'sync-committed': {
      const to = event.committedMode === 'single' ? 'single' : 'panes'
      if (to === state.committedMode && !state.transition) return state
      return { ...state, committedMode: to, transition: null }
    }

    // Topology mutation during a beat (a pane the exit is dealing out gets closed,
    // focus moves, a tab is deleted). INV 10: the descriptor cancels rather than
    // silently changing which element owns the animation. The controller decides
    // when a mutation invalidates the latched roles and dispatches this; it does
    // not touch committedMode (the durable mode already flipped).
    case 'cancel-beat': {
      return state.transition ? { ...state, transition: null } : state
    }

    // A keyed completion fired (animationend / finished / reduced-motion sync /
    // visibility reconcile). INV 15: a completion for id=N is a no-op unless N is
    // the LIVE transition, so a superseded epoch's late completion can never clear
    // the current one.
    case 'complete': {
      const t = state.transition
      if (!t || t.id !== event.id) return state
      return { ...state, transition: null }
    }

    default:
      return state
  }
}

// ── Pure derivations — the single source for every mode-dependent render fact
// (INV 4). Shell.jsx reads THESE, never a scattered boolean. Each takes the
// splits kill switch so the clamp (INV 16) is applied before anything else.

// INV 16: with splits disabled the presentation is clamped to single BEFORE any
// other derivation, so a persisted 'panes' blob can never reach the tiled render
// and strand the owner in a control-less builder view.
function clampMode(committedMode, splitsEnabled) {
  if (!splitsEnabled) return 'single'
  return committedMode
}

// The mode the render actually paints. During an exit or a drag preview the
// outgoing tiled world is held ('panes'); everything else is the committed mode.
// This is the ONE formula the old code smeared across an expression plus a raw
// renderTabRects check plus the root-class emitters (the P0 disagreement). INV 4.
export function effectiveViewMode(state, { splitsEnabled = true } = {}) {
  if (!splitsEnabled) return 'single'
  const t = state.transition
  if (t && (t.phase === 'exiting' || t.phase === 'drag-preview')) return 'panes'
  return clampMode(state.committedMode, splitsEnabled)
}

// The transient root class the shell wears for this beat. Exactly one, ever (INV
// 1). Empty string when idle or during a drag preview (a preview shows the tiled
// world but wears no enter/exit deal class).
export function transitionRootClass(state, { splitsEnabled = true } = {}) {
  if (!splitsEnabled) return ''
  const t = state.transition
  if (!t) return ''
  if (t.phase === 'entering') return 'shell--builder-entering'
  if (t.phase === 'exiting') return 'shell--builder-exiting'
  return ''
}

// Builder mode is active (the logo's 180° twist + the living halo + power chrome)
// whenever the COMMITTED mode is panes — it flips synchronously with the toggle,
// matching the gesture's own spring/snap. Clamped off by the kill switch. INV 4,
// INV 16.
export function builderModeActive(state, { splitsEnabled = true } = {}) {
  return clampMode(state.committedMode, splitsEnabled) === 'panes'
}

// The exit beat is holding the tiled geometry (drives renderTabRects widening the
// settling pane to the full box). INV 4, INV 9.
export function exitGeometryActive(state, { splitsEnabled = true } = {}) {
  if (!splitsEnabled) return false
  const t = state.transition
  return !!t && t.phase === 'exiting'
}

// A single-mode drag is previewing the builder world. INV 5.
export function dragPreviewActive(state, { splitsEnabled = true } = {}) {
  if (!splitsEnabled) return false
  const t = state.transition
  return !!t && t.phase === 'drag-preview'
}

// The role a given pane plays in the current exit beat, latched at exit start
// (INV 9). 'leaving' panes deal out and are made INERT (pointer + keyboard) by
// the render so a tap on a nearly-invisible leaving surface cannot re-target the
// transition (the P1 "chrome invisible, not inert" finding). 'staying' is the
// settling pane. null when no exit beat is live.
export function paneExitRole(state, paneId) {
  const t = state.transition
  if (!t || t.phase !== 'exiting') return null
  if (t.leavingPaneIds.includes(paneId)) return 'leaving'
  return 'staying'
}

// True while any exit beat is live — the render uses this to make leaving
// surfaces + workspace chrome pointer-inert and aria-hidden until settlement
// (INV 9). Distinct from exitGeometryActive only in intent (this one gates
// interactivity, that one gates geometry); kept separate so each reads at its
// call site.
export function leavingSurfacesInert(state) {
  const t = state.transition
  return !!t && t.phase === 'exiting'
}

// The completion contract for the live transition (INV 12): which animation-names
// end it, and the max duration the reconcile clock (INV 14) allows before it
// force-completes. Returns null when there is nothing to complete.
export function completionContract(state) {
  const t = state.transition
  if (!t || !t.animated) return null
  if (t.phase === 'entering') {
    return { id: t.id, animationNames: ENTER_ANIMATIONS, maxMs: MODE_ENTER_MS }
  }
  if (t.phase === 'exiting') {
    return { id: t.id, animationNames: EXIT_ANIMATIONS, maxMs: MODE_EXIT_MS }
  }
  return null // a drag preview has no self-completing beat
}

// Whether a live transition must complete SYNCHRONOUSLY because it carries no
// animation (reduced motion collapsed it, or there is no valid animation target).
// The controller checks this after committing and dispatches complete{id} in the
// same layout phase (INV 13). A transition with animated:true but no target is
// caught by the controller's target probe, not here.
export function needsSyncCompletion(state) {
  const t = state.transition
  return !!t && t.animated === false && t.phase !== 'drag-preview'
}

// INV 14: on visibilitychange→visible / pageshow, decide whether the live beat
// has already outlived its nominal duration (its animationend was throttled away
// while hidden) and should be force-completed. Returns a complete{id} event or
// null — a pure comparison against startedAt, never a scheduled timer.
export function reconcileVisibleEvent(state, now) {
  const contract = completionContract(state)
  if (!contract) return null
  const t = state.transition
  if (!Number.isFinite(now) || !Number.isFinite(t.startedAt)) return null
  if (now - t.startedAt >= contract.maxMs + RECONCILE_SLACK_MS) {
    return { type: 'complete', id: t.id }
  }
  return null
}

// True iff `animationName` under `epoch` is a valid completion signal for the
// live transition (INV 12 + INV 15): the epoch must be the live one AND the name
// must belong to the phase's animation set. The controller's animationend
// listener calls this before dispatching complete.
export function isCompletionSignal(state, epoch, animationName) {
  const contract = completionContract(state)
  if (!contract) return false
  if (contract.id !== epoch) return false // stale epoch (INV 15)
  return contract.animationNames.has(animationName)
}
