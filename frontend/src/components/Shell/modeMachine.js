// The mode-transition machine — the ONE descriptor that replaces the old
// three-boolean + two-timer + shared-render-override tangle (builderEntering,
// builderExiting, dragPreviewBuilder, builderEnterTimerRef, builderExitTimerRef
// and the effectiveViewMode expression scattered across Shell.jsx).
//
// Codex's adversarial review (codex-mode-machinery-review.md §3) found that the
// old shape "cannot be made sequence-proof with local guards": rapid re-entry,
// enter-during-exit, drag-arm-during-beat, undo-during-beat each stranded an
// independent flag/timer/class. The prescribed replacement is a single exclusive
// transition descriptor from which EVERYTHING derives, with completion keyed to the
// transition epoch rather than a bare timer.
//
// EXIT-PRESENTATION v2 reshape: the descriptor no longer carries role plumbing
// (focusedPaneId / leavingPaneIds / an `animated` boolean / fixed per-phase
// animation-name sets). It latches ONE opaque `presentation` plan built by
// workspaceView.deriveExit/EnterPlan. The machine treats the plan as data: it holds
// the outgoing world, rejects stale epochs, and exposes the completion contract the
// plan contains (its animation names + totalMs). It does NOT know what chat, app,
// Settings, zoom, or a future surface means — a new destination surface needs only a
// deriveExitPlan target adapter plus renderer support, never a new machine branch.
//
// State shape:
//
//   { committedMode: 'single' | 'panes',
//     transition: null | {
//       id,               // monotonic epoch — the completion key (INV 15)
//       phase,            // 'entering' | 'exiting' | 'drag-preview'
//       from, to,         // 'single' | 'panes'
//       cause,            // 'hold'|'swipe'|'keyboard'|'drag'|'undo'|'auto'|'toggle'
//       startedAt,        // ms — the reconcile clock (INV 14)
//       presentation,     // opaque latched plan (null for a drag preview)
//     } }
//
// The invariants are cited inline as `INV N` at their enforcement point so a
// reviewer can grep any invariant number to its code. (Numbering follows the
// exit-presentation v2 "Single invariant list for tests".)
//
//  1. One descriptor: at most one transition; no parallel booleans/timers.
//  2. Opaque latch: the transition owns one immutable presentation snapshot. Live
//     focus, topology, target, and geometry cannot retarget it.
//  3. New input supersedes the prior epoch synchronously.
//  4. effectiveViewMode, root class, logo/halo, aria, interactivity all derive from
//     this one descriptor.
//  5. Drag arm is phase 'drag-preview'; cancel and commit carry its id.
//  7. One completion contract: the plan's names + totalMs end the beat; a stale
//     epoch's completion cannot clear a newer one.
// 10. Invalidation snaps: a topology/geometry change cancels the beat (cancel-beat);
//     no transform is retargeted.
// 11. Reduced motion / an empty plan never arms a descriptor (instant flip).
// 14. visibilitychange / pageshow reconciles from startedAt; no bare timer.
// 16. The splits kill switch clamps presentation to single before derivation.

// The slack a visibility-return reconcile allows past the plan's nominal totalMs
// before it force-completes a beat whose animationend never arrived (hidden tab,
// throttled rAF). Not a correctness timer — nothing fires it on its own; it is a
// pure comparison against startedAt at the reconcile boundary (INV 14). Sourced
// from the presentation module so timing lives in one place.
export { MODE_MOTION, RECONCILE_SLACK_MS } from './workspaceView.js'
import { RECONCILE_SLACK_MS } from './workspaceView.js'

export function initialModeState(committedMode = 'panes') {
  return {
    committedMode: committedMode === 'single' ? 'single' : 'panes',
    transition: null,
    // Monotonic epoch source. Every transition takes `nextId` and increments it,
    // so no two transitions in a session ever share an id — the guarantee INV 7
    // (a stale completion from id=N cannot clear id=N+1) rests on.
    nextId: 1,
  }
}

// A presentation plan arms a beat iff it names at least one completion animation.
// Reduced motion and structurally-instant flips (an empty tree) are represented as
// a NULL presentation from the caller — there is no `animated` boolean to strand
// (INV 11: retained transitions are animated by construction). A drag preview
// passes no presentation and never self-completes.
function planArms(presentation) {
  return !!presentation
    && Array.isArray(presentation.completionNames)
    && presentation.completionNames.length > 0
}

// Build a fresh transition descriptor for a committed direction change, taking
// (and consuming) the monotonic epoch. Returns { transition, nextId }.
function makeTransition(state, { from, to, cause, presentation, now }) {
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
      presentation, // the opaque latched plan (INV 2)
    },
  }
}

// The pure reducer. Every mode-changing path in the app funnels here (INV 2);
// the controller (useModeController) is the only caller and pairs each
// mode-committing event with its workspace dispatch so the pair is one
// transaction. A toggle to the mode already committed with no live beat is a
// genuine no-op reference so React can bail.
export function modeReducer(state, event) {
  switch (event.type) {
    // A user toggle (hold / swipe / keyboard), a drop-flip, or the last-tab-close
    // auto-return (cause 'auto'). Direction is decided by the CURRENT committedMode
    // unless `to` is given explicitly (undo passes it). committedMode flips
    // SYNCHRONOUSLY here; the render holds the outgoing world during an exit beat
    // via effectiveViewMode, never by deferring the durable flip. INV 2, INV 3.
    case 'toggle': {
      const from = state.committedMode
      const to = event.to || (from === 'single' ? 'panes' : 'single')
      if (to === from && !state.transition) return state
      if (!planArms(event.presentation)) {
        // Instant flip — reduced motion, an empty tree, or a caller-declared
        // no-beat direction change. Any prior transition is superseded (INV 3/11).
        return { ...state, committedMode: to, transition: null }
      }
      const { transition, nextId } = makeTransition(state, {
        from, to, cause: event.cause, presentation: event.presentation, now: event.now,
      })
      // INV 3: a new toggle REPLACES whatever transition was live with a new epoch,
      // synchronously. The old epoch's pending completion is now stale (INV 7).
      return { committedMode: to, transition, nextId }
    }

    // A single-mode drag previews the builder world without committing the durable
    // mode (design: "dragging is building" — the preview, not the commit). INV 5.
    // Only meaningful from single. Supersedes any live beat. INV 3.
    case 'drag-arm': {
      if (state.committedMode !== 'single') {
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
          presentation: null, // the preview has no self-completing plan
        },
        nextId: id + 1,
      }
    }

    // Cancel/Escape/blur/lost-capture end of a drag preview. INV 5/7: only the
    // matching live drag-preview epoch is cleared — a stale cancel is ignored.
    case 'drag-cancel': {
      const t = state.transition
      if (!t || t.phase !== 'drag-preview' || t.id !== event.id) return state
      return { ...state, transition: null }
    }

    // A drop committed. The workspace reducer performs the placement + the 'panes'
    // flip in the SAME batch; here we reflect committedMode='panes' and drop the
    // preview. Only the matching live epoch commits (INV 5/7). A rejected/no-op drop
    // never reaches here, so this never fabricates a mode change (P1 review finding).
    case 'drag-commit': {
      const t = state.transition
      if (!t || t.phase !== 'drag-preview' || t.id !== event.id) return state
      return { ...state, committedMode: 'panes', transition: null }
    }

    // Undo restored the durable viewMode. Undo is a first-class controller event
    // (INV 2) rather than a silent bypass. A restored mode that differs from the
    // committed one is a real direction change and earns a beat when the caller
    // supplies a plan (undoing a last-tab-close auto-return re-enters builder with
    // the entry deal); an ordinary tree undo carries the current mode forward, so
    // restoredMode === committedMode and this is a no-op.
    case 'undo': {
      const to = event.restoredMode === 'single' ? 'single' : 'panes'
      const from = state.committedMode
      if (to === from) {
        return state.transition ? { ...state, transition: null } : state
      }
      if (!planArms(event.presentation)) {
        return { ...state, committedMode: to, transition: null }
      }
      const { transition, nextId } = makeTransition(state, {
        from, to, cause: 'undo', presentation: event.presentation, now: event.now,
      })
      return { committedMode: to, transition, nextId }
    }

    // Reconcile committedMode with an EXTERNAL durable-mode change — hydration on
    // boot/reload, or any viewMode change that did not originate here. A reload
    // starts directly in the durable mode with NO beat (the accepted reload
    // policy), so this always clears any transition. Same reference when in sync.
    case 'sync-committed': {
      const to = event.committedMode === 'single' ? 'single' : 'panes'
      if (to === state.committedMode && !state.transition) return state
      return { ...state, committedMode: to, transition: null }
    }

    // Topology / geometry mutation invalidated the latched plan (a participant pane
    // closed, the active tab changed, the content box resized). INV 10: the
    // descriptor CANCELS rather than silently retargeting a transform. The
    // controller detects the snapshot drift and dispatches this; it does not touch
    // committedMode (the durable mode already flipped).
    case 'cancel-beat': {
      return state.transition ? { ...state, transition: null } : state
    }

    // A keyed completion fired (finished promise / reduced-motion sync / visibility
    // reconcile). INV 7: a completion for id=N is a no-op unless N is the LIVE
    // transition, so a superseded epoch's late completion can never clear the
    // current one.
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

// Builder mode is active (the logo's 180° twist + power chrome) whenever the
// COMMITTED mode is panes — it flips synchronously with the toggle. The LIVING HALO
// pauses during any beat (see Shell's haloActive), so this is builder chrome, not
// the halo gate. Clamped off by the kill switch. INV 4/16.
export function builderModeActive(state, { splitsEnabled = true } = {}) {
  return clampMode(state.committedMode, splitsEnabled) === 'panes'
}

// True while an exit beat is live — the render uses this to (a) render the underlay,
// (b) make outgoing panes + chrome + the underlay inert, and (c) pass every visible
// AppCanvas interactive:false (INV 9 inert beat). Replaces the old
// exitGeometryActive + leavingSurfacesInert pair.
export function exitBeatActive(state, { splitsEnabled = true } = {}) {
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

// The latched presentation plan for the live ANIMATED beat (entering/exiting), or
// null. Shell reads this to apply each participant's data-mode-motion + inline
// FLIP/duration/delay variables. A drag preview has no plan.
export function transitionPresentation(state) {
  const t = state.transition
  if (!t || t.phase === 'drag-preview') return null
  return t.presentation || null
}

// The latched EXIT plan (target + participants + underlayKey), or null. Shell reads
// underlayKey/target to paint the revealed destination beneath the deal.
export function exitPresentation(state) {
  const t = state.transition
  if (!t || t.phase !== 'exiting') return null
  return t.presentation || null
}

// The completion contract for the live transition (INV 7): which animation-names
// end it, and the max duration the reconcile clock (INV 14) allows before it
// force-completes. Taken straight from the latched plan. Null when nothing pends.
export function completionContract(state) {
  const p = transitionPresentation(state)
  if (!p) return null
  const t = state.transition
  return {
    id: t.id,
    animationNames: new Set(p.completionNames),
    maxMs: p.totalMs,
  }
}

// INV 14: on visibilitychange→visible / pageshow, decide whether the live beat has
// already outlived its nominal duration (its animationend was throttled away while
// hidden) and should be force-completed. Returns a complete{id} event or null — a
// pure comparison against startedAt, never a scheduled timer.
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
