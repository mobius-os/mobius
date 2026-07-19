import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  initialModeState, modeReducer,
  effectiveViewMode, transitionRootClass, builderModeActive,
  exitGeometryActive, dragPreviewActive, paneExitRole, leavingSurfacesInert,
  completionContract, needsSyncCompletion, reconcileVisibleEvent, isCompletionSignal,
  MODE_ENTER_MS, MODE_EXIT_MS, RECONCILE_SLACK_MS,
} from '../modeMachine.js'

// The mandatory fix test matrix (codex-mode-machinery-review.md §4) over the pure
// transition machine. Every wedge sequence the review catalogs (§1-2) has a case
// here. After each step we assert the review's checklist facts: committed mode,
// effective/presented mode, exactly one-or-zero transition phase/class, latched
// exit roles, completion contract, and eventual idle with no residue.

const KEEP = { multiPane: true } // a tiled workspace (an exit earns the deal)
const FLAT = { multiPane: false } // a single leaf (an exit is instant)

function single() { return { ...initialModeState('single'), nextId: 1 } }
function panes() { return { ...initialModeState('panes'), nextId: 1 } }

// Assert the descriptor is idle: no transition, no root class, geometry settled.
function assertIdle(state, mode) {
  assert.equal(state.transition, null, 'no live transition')
  assert.equal(transitionRootClass(state), '', 'no transient root class')
  assert.equal(exitGeometryActive(state), false)
  assert.equal(dragPreviewActive(state), false)
  assert.equal(effectiveViewMode(state), mode)
  assert.equal(state.committedMode, mode)
}

// ── Baseline: the two stable modes ──────────────────────────────────────────

test('stable single: idle, presents single, builder off', () => {
  const s = single()
  assertIdle(s, 'single')
  assert.equal(builderModeActive(s), false)
})

test('stable panes: idle, presents panes, builder on', () => {
  const s = panes()
  assertIdle(s, 'panes')
  assert.equal(builderModeActive(s), true)
})

// ── Enter (single → panes) ──────────────────────────────────────────────────

test('enter: committed flips to panes synchronously, entering class, one epoch', () => {
  const s = modeReducer(single(), { type: 'toggle', cause: 'hold', ...FLAT, now: 100 })
  assert.equal(s.committedMode, 'panes', 'durable mode flips at once (no deferred flip)')
  assert.equal(s.transition.phase, 'entering')
  assert.equal(s.transition.id, 1)
  assert.equal(effectiveViewMode(s), 'panes')
  assert.equal(transitionRootClass(s), 'shell--builder-entering')
  assert.equal(builderModeActive(s), true)
  // enter completes on an entry animation.
  const c = completionContract(s)
  assert.equal(c.maxMs, MODE_ENTER_MS)
  assert.ok(c.animationNames.has('shell-strip-deal-in'))
})

test('enter then complete: settles to stable panes, no residue', () => {
  let s = modeReducer(single(), { type: 'toggle', ...FLAT, now: 0 })
  s = modeReducer(s, { type: 'complete', id: s.transition.id })
  assertIdle(s, 'panes')
})

test('enter under reduced motion: instant, no beat', () => {
  const s = modeReducer(single(), { type: 'toggle', reducedMotion: true, ...FLAT })
  assert.equal(s.committedMode, 'panes')
  assert.equal(s.transition, null)
  assert.equal(needsSyncCompletion(s), false) // nothing pending — it never armed
})

// ── Exit (panes → single) ───────────────────────────────────────────────────

test('exit tiled: committed flips to single, render holds panes for the deal', () => {
  const s = modeReducer(panes(), {
    type: 'toggle', cause: 'hold', ...KEEP, focusedPaneId: 'p1', leavingPaneIds: ['p2'], now: 50,
  })
  assert.equal(s.committedMode, 'single', 'durable mode is already single')
  assert.equal(effectiveViewMode(s), 'panes', 'but the render holds the tiled world')
  assert.equal(transitionRootClass(s), 'shell--builder-exiting')
  assert.equal(exitGeometryActive(s), true)
  assert.equal(leavingSurfacesInert(s), true)
  // Latched roles (INV 9).
  assert.equal(paneExitRole(s, 'p2'), 'leaving')
  assert.equal(paneExitRole(s, 'p1'), 'staying')
  assert.equal(s.transition.focusedPaneId, 'p1')
  const c = completionContract(s)
  assert.equal(c.maxMs, MODE_EXIT_MS)
  assert.ok(c.animationNames.has('shell-pane-deal-out'))
})

test('exit single leaf: instant collapse, no deal', () => {
  const s = modeReducer(panes(), { type: 'toggle', ...FLAT })
  assert.equal(s.committedMode, 'single')
  assert.equal(s.transition, null)
  assertIdle(s, 'single')
})

test('exit then complete: settles to single, no residue', () => {
  let s = modeReducer(panes(), { type: 'toggle', ...KEEP, leavingPaneIds: ['p2'], now: 0 })
  s = modeReducer(s, { type: 'complete', id: s.transition.id })
  assertIdle(s, 'single')
})

// ── P0: rapid re-entry invalidates only effectiveViewMode — REGRESSION GUARD ──
// The exact wedge the review opened with: exit armed, then re-enter before the
// beat completes. Old code left builderExiting latched so geometry/CSS disagreed.

test('rapid exit→enter before completion: exit epoch fully superseded', () => {
  let s = modeReducer(panes(), {
    type: 'toggle', ...KEEP, focusedPaneId: 'p1', leavingPaneIds: ['p2'], now: 0,
  })
  const exitId = s.transition.id
  // Re-enter within the beat.
  s = modeReducer(s, { type: 'toggle', ...FLAT, now: 120 })
  assert.equal(s.committedMode, 'panes')
  assert.equal(s.transition.phase, 'entering', 'now an entry, not a stranded exit')
  assert.notEqual(s.transition.id, exitId, 'new epoch')
  // Every derivation agrees: no exit geometry, no exit class survives.
  assert.equal(exitGeometryActive(s), false)
  assert.equal(effectiveViewMode(s), 'panes')
  assert.equal(transitionRootClass(s), 'shell--builder-entering')
  assert.equal(paneExitRole(s, 'p2'), null, 'no stale leaving role')
  // The stale exit completion can no longer clear the live entry (INV 15).
  const after = modeReducer(s, { type: 'complete', id: exitId })
  assert.equal(after.transition.phase, 'entering', 'stale completion ignored')
})

test('rapid enter→exit before completion: entry epoch fully superseded', () => {
  let s = modeReducer(single(), { type: 'toggle', ...FLAT, now: 0 })
  const enterId = s.transition.id
  s = modeReducer(s, { type: 'toggle', ...KEEP, leavingPaneIds: ['p2'], now: 100 })
  assert.equal(s.committedMode, 'single')
  assert.equal(s.transition.phase, 'exiting')
  assert.notEqual(s.transition.id, enterId)
  // Final single surface must NOT run an entry settle animation.
  assert.equal(transitionRootClass(s), 'shell--builder-exiting')
  const after = modeReducer(s, { type: 'complete', id: enterId })
  assert.equal(after.transition.phase, 'exiting', 'stale enter completion ignored')
})

// ── Toggle overlaps at every offset (matrix §Toggle overlaps) ────────────────

test('enter→exit→enter: each accepted transition gets a new epoch', () => {
  let s = single()
  const ids = []
  s = modeReducer(s, { type: 'toggle', ...FLAT, now: 0 }); ids.push(s.transition.id)
  s = modeReducer(s, { type: 'toggle', ...KEEP, leavingPaneIds: ['p2'], now: 10 }); ids.push(s.transition.id)
  s = modeReducer(s, { type: 'toggle', ...FLAT, now: 20 }); ids.push(s.transition.id)
  assert.deepEqual(ids, [1, 2, 3], 'monotonic epochs, one per accepted input')
  assert.equal(s.committedMode, 'panes')
  assert.equal(s.transition.phase, 'entering')
  // At most one transition ever exists (INV 1).
  assert.ok(s.transition && typeof s.transition === 'object')
})

test('repeated enter request while entering: re-arms with a fresh epoch (not a no-op latch)', () => {
  let s = modeReducer(single(), { type: 'toggle', ...FLAT, now: 0 })
  const first = s.transition.id
  // A second enter while already entering (committed already panes) — the old
  // code no-opped setBuilderEntering(true) and the animation never restarted.
  s = modeReducer(s, { type: 'toggle', to: 'panes', ...FLAT, now: 50 })
  assert.equal(s.committedMode, 'panes')
  assert.equal(s.transition.phase, 'entering')
  assert.notEqual(s.transition.id, first, 'entry animation restarts under a new epoch')
})

test('idempotent toggle to the committed mode with no beat is a no-op reference', () => {
  const s = panes()
  const after = modeReducer(s, { type: 'toggle', to: 'panes', ...FLAT })
  assert.equal(after, s, 'same reference — React can bail')
})

// ── Drag-preview overlaps (matrix §Drag-preview overlaps) ────────────────────

test('drag arm from single: drag-preview phase, committed stays single', () => {
  const s = modeReducer(single(), { type: 'drag-arm', now: 0 })
  assert.equal(s.committedMode, 'single')
  assert.equal(s.transition.phase, 'drag-preview')
  assert.equal(dragPreviewActive(s), true)
  assert.equal(effectiveViewMode(s), 'panes', 'preview paints the tiled world')
  assert.equal(transitionRootClass(s), '', 'a preview wears no enter/exit deal class')
  assert.equal(completionContract(s), null, 'a preview has no self-completing beat')
})

test('drag arm→cancel with matching id: back to idle single', () => {
  let s = modeReducer(single(), { type: 'drag-arm', now: 0 })
  const id = s.transition.id
  s = modeReducer(s, { type: 'drag-cancel', id })
  assertIdle(s, 'single')
})

test('drag arm→cancel with STALE id: ignored (INV 5/15)', () => {
  let s = modeReducer(single(), { type: 'drag-arm', now: 0 })
  const live = s.transition.id
  s = modeReducer(s, { type: 'drag-cancel', id: live + 99 })
  assert.equal(s.transition.phase, 'drag-preview', 'stale cancel did not end the live preview')
})

test('drag arm→commit: mode flips to panes, preview cleared, one transaction', () => {
  let s = modeReducer(single(), { type: 'drag-arm', now: 0 })
  const id = s.transition.id
  s = modeReducer(s, { type: 'drag-commit', id })
  assert.equal(s.committedMode, 'panes')
  assert.equal(s.transition, null, 'no lingering preview or fabricated entry beat')
  assertIdle(s, 'panes')
})

test('drag commit with STALE id: ignored — no fabricated mode change', () => {
  let s = modeReducer(single(), { type: 'drag-arm', now: 0 })
  s = modeReducer(s, { type: 'drag-commit', id: s.transition.id + 5 })
  assert.equal(s.committedMode, 'single', 'a stale/no-op drop mutates nothing (P1 finding)')
  assert.equal(s.transition.phase, 'drag-preview')
})

test('drag arm in BUILDER is not a preview (already tiled)', () => {
  const s = modeReducer(panes(), { type: 'drag-arm', now: 0 })
  assert.equal(s.transition, null, 'no preview transition in builder')
  assert.equal(s.committedMode, 'panes')
})

test('arm during entry: drag preview supersedes the entry beat', () => {
  let s = modeReducer(single(), { type: 'toggle', ...FLAT, now: 0 })
  const enterId = s.transition.id
  s = modeReducer(s, { type: 'drag-arm', now: 30 })
  // committed went single→panes on enter; arm requires single, so from panes this
  // is the builder branch: no preview, entry beat dropped.
  assert.equal(s.committedMode, 'panes')
  assert.equal(s.transition, null)
  const after = modeReducer(s, { type: 'complete', id: enterId })
  assert.equal(after.transition, null, 'stale entry completion cannot revive a beat')
})

test('arm during exit: exit committed single, so the arm previews builder', () => {
  let s = modeReducer(panes(), { type: 'toggle', ...KEEP, leavingPaneIds: ['p2'], now: 0 })
  const exitId = s.transition.id
  s = modeReducer(s, { type: 'drag-arm', now: 30 })
  assert.equal(s.committedMode, 'single')
  assert.equal(s.transition.phase, 'drag-preview', 'the exit beat is superseded by the preview')
  assert.notEqual(s.transition.id, exitId)
  assert.equal(exitGeometryActive(s), false, 'no stranded exit geometry')
})

// ── Single-mode auto-flip and Undo (matrix §Single-mode auto-flip and Undo) ──

test('auto-flip to single (last-tab-close) with a tiled tree: exit deal', () => {
  const s = modeReducer(panes(), {
    type: 'auto-flip', to: 'single', ...KEEP, focusedPaneId: 'p1', leavingPaneIds: ['p2'], now: 0,
  })
  assert.equal(s.committedMode, 'single')
  assert.equal(s.transition.phase, 'exiting')
  assert.equal(s.transition.cause, 'auto-flip')
})

test('undo restoring panes from single: re-enters builder with the entry deal', () => {
  const s = modeReducer(single(), {
    type: 'undo', restoredMode: 'panes', ...FLAT, now: 0,
  })
  assert.equal(s.committedMode, 'panes')
  assert.equal(s.transition.phase, 'entering')
  assert.equal(s.transition.cause, 'undo')
})

test('ordinary tree undo (mode unchanged): no beat, same committed mode', () => {
  const base = panes()
  const s = modeReducer(base, { type: 'undo', restoredMode: 'panes', ...FLAT })
  assert.equal(s, base, 'no-op reference — undo carried the current mode forward')
})

test('undo restoring single from panes tiled: exit deal', () => {
  const s = modeReducer(panes(), {
    type: 'undo', restoredMode: 'single', ...KEEP, leavingPaneIds: ['p2'], now: 0,
  })
  assert.equal(s.committedMode, 'single')
  assert.equal(s.transition.phase, 'exiting')
})

test('later explicit toggle supersedes a coupled undo beat (INV 8 spirit)', () => {
  // auto-flip → later toggle: the newer explicit intent owns the descriptor.
  let s = modeReducer(panes(), { type: 'auto-flip', to: 'single', ...KEEP, leavingPaneIds: ['p2'], now: 0 })
  s = modeReducer(s, { type: 'toggle', ...FLAT, now: 40 }) // back to panes
  assert.equal(s.committedMode, 'panes')
  assert.equal(s.transition.phase, 'entering')
})

// ── Exit interaction and topology (matrix §Exit interaction and topology) ────

test('topology mutation during exit cancels the beat (INV 10)', () => {
  let s = modeReducer(panes(), {
    type: 'toggle', ...KEEP, focusedPaneId: 'p1', leavingPaneIds: ['p2'], now: 0,
  })
  // A leaving pane gets deleted mid-beat → controller dispatches cancel-beat.
  s = modeReducer(s, { type: 'cancel-beat' })
  assert.equal(s.transition, null, 'beat cancelled, animation ownership not silently changed')
  assert.equal(s.committedMode, 'single', 'durable mode is unaffected')
  assertIdle(s, 'single')
})

test('exit latches its OWN copy of leavingPaneIds (INV 9)', () => {
  const caller = ['p2', 'p3']
  const s = modeReducer(panes(), {
    type: 'toggle', ...KEEP, focusedPaneId: 'p1', leavingPaneIds: caller, now: 0,
  })
  // Mutating the caller's array after the fact must not change the latched roles —
  // a topology change to the live pane list cannot silently rewrite who is leaving.
  caller.push('p4')
  assert.deepEqual(s.transition.leavingPaneIds, ['p2', 'p3'])
  assert.equal(paneExitRole(s, 'p4'), 'staying', 'a pane added after latch is not leaving')
  assert.equal(paneExitRole(s, 'p2'), 'leaving')
})

// ── Page lifecycle and motion (matrix §Page lifecycle and motion) ────────────

test('reduced motion collapses both directions to instant flips', () => {
  let s = modeReducer(single(), { type: 'toggle', reducedMotion: true, ...KEEP })
  assert.equal(s.transition, null)
  assert.equal(s.committedMode, 'panes')
  s = modeReducer(s, { type: 'toggle', reducedMotion: true, ...KEEP, leavingPaneIds: ['p2'] })
  assert.equal(s.transition, null)
  assert.equal(s.committedMode, 'single')
})

test('sync-committed reconciles an external durable change with no beat (reload policy)', () => {
  let s = modeReducer(single(), { type: 'toggle', ...FLAT, now: 0 }) // entering, committed panes
  // Reload hydrates a persisted single blob → controller syncs.
  s = modeReducer(s, { type: 'sync-committed', committedMode: 'single' })
  assert.equal(s.committedMode, 'single')
  assert.equal(s.transition, null, 'reload starts in the durable mode with no beat')
})

test('visibility reconcile force-completes a beat that outlived its duration (INV 14)', () => {
  const s = modeReducer(panes(), { type: 'toggle', ...KEEP, leavingPaneIds: ['p2'], now: 1000 })
  // Return to the tab well after the exit could have finished.
  const late = 1000 + MODE_EXIT_MS + RECONCILE_SLACK_MS + 5
  const ev = reconcileVisibleEvent(s, late)
  assert.ok(ev, 'an overdue beat yields a completion event')
  assert.equal(ev.type, 'complete')
  assert.equal(ev.id, s.transition.id)
  const done = modeReducer(s, ev)
  assertIdle(done, 'single')
})

test('visibility reconcile leaves a still-fresh beat running', () => {
  const s = modeReducer(panes(), { type: 'toggle', ...KEEP, leavingPaneIds: ['p2'], now: 1000 })
  const ev = reconcileVisibleEvent(s, 1000 + 10) // barely started
  assert.equal(ev, null, 'a fresh beat is not force-completed')
})

// ── Animation lifecycle (matrix §Animation lifecycle) ────────────────────────

test('completion signal filters by epoch AND animation name (INV 12)', () => {
  const s = modeReducer(single(), { type: 'toggle', ...FLAT, now: 0 })
  const id = s.transition.id
  assert.equal(isCompletionSignal(s, id, 'shell-strip-deal-in'), true)
  assert.equal(isCompletionSignal(s, id, 'shell-pane-deal-out'), false, 'wrong phase animation')
  assert.equal(isCompletionSignal(s, id, 'workspace-sheet-in'), false, 'unrelated animation')
  assert.equal(isCompletionSignal(s, id + 1, 'shell-strip-deal-in'), false, 'stale epoch')
})

test('duplicate completion is harmless', () => {
  let s = modeReducer(single(), { type: 'toggle', ...FLAT, now: 0 })
  const id = s.transition.id
  s = modeReducer(s, { type: 'complete', id })
  const again = modeReducer(s, { type: 'complete', id })
  assert.equal(again, s, 'a second completion is a no-op reference')
})

test('completion from a superseded epoch never clears a newer transition (INV 15)', () => {
  let s = modeReducer(single(), { type: 'toggle', ...FLAT, now: 0 })
  const oldId = s.transition.id
  s = modeReducer(s, { type: 'toggle', ...KEEP, leavingPaneIds: ['p2'], now: 50 })
  const newId = s.transition.id
  const after = modeReducer(s, { type: 'complete', id: oldId })
  assert.equal(after.transition.id, newId, 'live transition untouched by the old completion')
})

test('needsSyncCompletion is false for animated beats and drag previews', () => {
  const enter = modeReducer(single(), { type: 'toggle', ...FLAT, now: 0 })
  assert.equal(needsSyncCompletion(enter), false)
  const preview = modeReducer(single(), { type: 'drag-arm', now: 0 })
  assert.equal(needsSyncCompletion(preview), false)
})

// ── Kill-switch clamp (matrix §Baseline: splits flag on/off; INV 16) ─────────

test('kill switch clamps presentation to single before any derivation', () => {
  const s = panes() // durable panes, but splits disabled
  const opts = { splitsEnabled: false }
  assert.equal(effectiveViewMode(s, opts), 'single', 'a persisted panes blob never reaches tiled render')
  assert.equal(builderModeActive(s, opts), false)
  assert.equal(transitionRootClass(s, opts), '')
  assert.equal(exitGeometryActive(s, opts), false)
  assert.equal(dragPreviewActive(s, opts), false)
})

test('kill switch clamps even a live entering transition', () => {
  const s = modeReducer(single(), { type: 'toggle', ...FLAT, now: 0 })
  assert.equal(effectiveViewMode(s, { splitsEnabled: false }), 'single')
  assert.equal(transitionRootClass(s, { splitsEnabled: false }), '')
})

// ── Every-input sanity: unknown events are no-ops ────────────────────────────

test('an unknown event is a no-op reference', () => {
  const s = panes()
  assert.equal(modeReducer(s, { type: 'nonsense' }), s)
})
