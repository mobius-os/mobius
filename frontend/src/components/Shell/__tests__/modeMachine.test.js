import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  initialModeState, modeReducer,
  effectiveViewMode, transitionRootClass, builderModeActive,
  exitBeatActive, dragPreviewActive,
  transitionPresentation, exitPresentation,
  completionContract, reconcileVisibleEvent,
  RECONCILE_SLACK_MS, MODE_MOTION,
} from '../modeMachine.js'

// The exit-presentation v2 invariant lock (the "Single invariant list for tests").
// Every transition latches ONE opaque presentation plan; the machine holds the
// outgoing world, rejects stale epochs, and exposes the plan's completion contract.
// It has NO role plumbing, no `animated` boolean, and no fixed per-phase animation
// sets — a plan arms a beat iff it names a completion animation. INV numbers follow
// modeMachine.js.

// A minimal enter plan (one deal-in participant).
function enterPlan({ totalMs = MODE_MOTION.enterItemMs } = {}) {
  return {
    kind: 'enter',
    participants: [{ key: 'chat:1', paneId: 'p1', motion: 'deal-in', delayMs: 0, durationMs: totalMs }],
    completionNames: ['shell-mode-deal-in'],
    totalMs,
  }
}
// A minimal exit plan (one deal-out participant, a chat underlay).
function exitPlan({ underlayKey = 'chat:5', target = 'chat:5', sig = 'sigA', totalMs = MODE_MOTION.exitItemMs } = {}) {
  return {
    kind: 'exit',
    target,
    destinationRect: { x: 0, y: 0, w: 100, h: 200 },
    participants: [{ key: 'chat:2', paneId: 'p2', motion: 'deal-out', delayMs: 0, durationMs: totalMs }],
    underlayKey,
    completionNames: ['shell-mode-deal-out'],
    totalMs,
    snapshotSignature: sig,
  }
}
// A promote exit plan (physical continuity — no underlay).
function promotePlan({ sig = 'sigP' } = {}) {
  return {
    kind: 'exit',
    target: 'app:9',
    destinationRect: { x: 0, y: 0, w: 100, h: 200 },
    participants: [{ key: 'app:9', paneId: 'p1', motion: 'promote', delayMs: 0, durationMs: MODE_MOTION.promoteMs, flip: { x: 0, y: 0, sx: 1, sy: 1 } }],
    underlayKey: null,
    completionNames: ['shell-mode-promote'],
    totalMs: MODE_MOTION.promoteMs,
    snapshotSignature: sig,
  }
}

function single() { return { ...initialModeState('single'), nextId: 1 } }
function panes() { return { ...initialModeState('panes'), nextId: 1 } }

function assertIdle(state, mode) {
  assert.equal(state.transition, null, 'no live transition')
  assert.equal(transitionRootClass(state), '', 'no transient root class')
  assert.equal(exitBeatActive(state), false)
  assert.equal(dragPreviewActive(state), false)
  assert.equal(transitionPresentation(state), null)
  assert.equal(exitPresentation(state), null)
  assert.equal(completionContract(state), null)
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

// ── INV 2 (opaque latch) + Enter ────────────────────────────────────────────

test('enter: committed flips to panes synchronously, entering class, one epoch, plan latched', () => {
  const plan = enterPlan()
  const s = modeReducer(single(), { type: 'toggle', cause: 'hold', presentation: plan, now: 100 })
  assert.equal(s.committedMode, 'panes', 'durable mode flips at once (no deferred flip)')
  assert.equal(s.transition.phase, 'entering')
  assert.equal(s.transition.id, 1)
  assert.equal(s.transition.presentation, plan, 'the EXACT plan reference is latched, opaquely')
  assert.equal(effectiveViewMode(s), 'panes')
  assert.equal(transitionRootClass(s), 'shell--builder-entering')
  assert.equal(builderModeActive(s), true)
  assert.equal(transitionPresentation(s), plan)
  // Completion contract is taken STRAIGHT from the plan (INV 7): names + totalMs.
  const c = completionContract(s)
  assert.equal(c.maxMs, plan.totalMs)
  assert.ok(c.animationNames.has('shell-mode-deal-in'))
})

test('enter then complete: settles to stable panes, no residue', () => {
  let s = modeReducer(single(), { type: 'toggle', presentation: enterPlan(), now: 0 })
  s = modeReducer(s, { type: 'complete', id: s.transition.id })
  assertIdle(s, 'panes')
})

test('a null presentation is an instant flip — reduced motion / empty tree never arms (INV 11)', () => {
  const s = modeReducer(single(), { type: 'toggle', presentation: null })
  assert.equal(s.committedMode, 'panes')
  assert.equal(s.transition, null)
})

test('a plan with NO completion names does not arm (INV 11)', () => {
  const empty = { kind: 'exit', participants: [], completionNames: [], totalMs: 0, underlayKey: null, target: null, snapshotSignature: 's' }
  const s = modeReducer(panes(), { type: 'toggle', presentation: empty })
  assert.equal(s.transition, null, 'no participants → instant flip')
  assert.equal(s.committedMode, 'single')
})

// ── Exit (panes → single): world reveal + promote ───────────────────────────

test('exit reveal: committed flips to single, render holds panes, underlay + target exposed', () => {
  const plan = exitPlan({ underlayKey: 'chat:5', target: 'chat:5' })
  const s = modeReducer(panes(), { type: 'toggle', cause: 'hold', presentation: plan, now: 50 })
  assert.equal(s.committedMode, 'single', 'durable mode is already single')
  assert.equal(effectiveViewMode(s), 'panes', 'but the render holds the tiled world')
  assert.equal(transitionRootClass(s), 'shell--builder-exiting')
  assert.equal(exitBeatActive(s), true)
  assert.equal(exitPresentation(s), plan)
  assert.equal(exitPresentation(s).underlayKey, 'chat:5')
  const c = completionContract(s)
  assert.equal(c.maxMs, plan.totalMs)
  assert.ok(c.animationNames.has('shell-mode-deal-out'))
})

test('exit promote: physical continuity, no underlay, promote completion name', () => {
  const plan = promotePlan()
  const s = modeReducer(panes(), { type: 'toggle', presentation: plan, now: 0 })
  assert.equal(exitPresentation(s).underlayKey, null)
  assert.ok(completionContract(s).animationNames.has('shell-mode-promote'))
})

test('exit then complete: settles to single, no residue', () => {
  let s = modeReducer(panes(), { type: 'toggle', presentation: exitPlan(), now: 0 })
  s = modeReducer(s, { type: 'complete', id: s.transition.id })
  assertIdle(s, 'single')
})

// ── INV 3 (supersession) — rapid re-entry ───────────────────────────────────

test('rapid exit→enter before completion: exit epoch fully superseded (INV 3/7)', () => {
  let s = modeReducer(panes(), { type: 'toggle', presentation: exitPlan(), now: 0 })
  const exitId = s.transition.id
  s = modeReducer(s, { type: 'toggle', presentation: enterPlan(), now: 120 })
  assert.equal(s.committedMode, 'panes')
  assert.equal(s.transition.phase, 'entering', 'now an entry, not a stranded exit')
  assert.notEqual(s.transition.id, exitId, 'new epoch')
  assert.equal(exitBeatActive(s), false)
  assert.equal(effectiveViewMode(s), 'panes')
  assert.equal(transitionRootClass(s), 'shell--builder-entering')
  // The stale exit completion can no longer clear the live entry (INV 7).
  const after = modeReducer(s, { type: 'complete', id: exitId })
  assert.equal(after.transition.phase, 'entering', 'stale completion ignored')
})

test('rapid enter→exit before completion: entry epoch fully superseded', () => {
  let s = modeReducer(single(), { type: 'toggle', presentation: enterPlan(), now: 0 })
  const enterId = s.transition.id
  s = modeReducer(s, { type: 'toggle', presentation: exitPlan(), now: 100 })
  assert.equal(s.committedMode, 'single')
  assert.equal(s.transition.phase, 'exiting')
  assert.notEqual(s.transition.id, enterId)
  const after = modeReducer(s, { type: 'complete', id: enterId })
  assert.equal(after.transition.phase, 'exiting', 'stale enter completion ignored')
})

test('enter→exit→enter: each accepted transition gets a new monotonic epoch', () => {
  let s = single()
  const ids = []
  s = modeReducer(s, { type: 'toggle', presentation: enterPlan(), now: 0 }); ids.push(s.transition.id)
  s = modeReducer(s, { type: 'toggle', presentation: exitPlan(), now: 10 }); ids.push(s.transition.id)
  s = modeReducer(s, { type: 'toggle', presentation: enterPlan(), now: 20 }); ids.push(s.transition.id)
  assert.deepEqual(ids, [1, 2, 3], 'monotonic epochs, one per accepted input')
  assert.equal(s.committedMode, 'panes')
  assert.equal(s.transition.phase, 'entering')
})

test('repeated enter request while entering: re-arms with a fresh epoch', () => {
  let s = modeReducer(single(), { type: 'toggle', presentation: enterPlan(), now: 0 })
  const first = s.transition.id
  s = modeReducer(s, { type: 'toggle', to: 'panes', presentation: enterPlan(), now: 50 })
  assert.equal(s.committedMode, 'panes')
  assert.equal(s.transition.phase, 'entering')
  assert.notEqual(s.transition.id, first, 'entry animation restarts under a new epoch')
})

test('cause threads through; omitting it falls back to the generic label', () => {
  const p = enterPlan()
  assert.equal(modeReducer(single(), { type: 'toggle', cause: 'hold', presentation: p, now: 0 }).transition.cause, 'hold')
  assert.equal(modeReducer(single(), { type: 'toggle', cause: 'swipe', presentation: p, now: 0 }).transition.cause, 'swipe')
  assert.equal(modeReducer(single(), { type: 'toggle', cause: 'keyboard', presentation: p, now: 0 }).transition.cause, 'keyboard')
  assert.equal(modeReducer(panes(), { type: 'toggle', cause: 'auto', to: 'single', presentation: exitPlan(), now: 0 }).transition.cause, 'auto')
  assert.equal(modeReducer(single(), { type: 'toggle', presentation: p, now: 0 }).transition.cause, 'toggle')
})

test('idempotent toggle to the committed mode with no beat is a no-op reference', () => {
  const s = panes()
  const after = modeReducer(s, { type: 'toggle', to: 'panes', presentation: null })
  assert.equal(after, s, 'same reference — React can bail')
})

// ── INV 5: Drag-preview overlaps ────────────────────────────────────────────

test('drag arm from single: drag-preview phase, committed stays single, no plan', () => {
  const s = modeReducer(single(), { type: 'drag-arm', now: 0 })
  assert.equal(s.committedMode, 'single')
  assert.equal(s.transition.phase, 'drag-preview')
  assert.equal(dragPreviewActive(s), true)
  assert.equal(effectiveViewMode(s), 'panes', 'preview paints the tiled world')
  assert.equal(transitionRootClass(s), '', 'a preview wears no enter/exit deal class')
  assert.equal(transitionPresentation(s), null, 'a preview has no plan')
  assert.equal(completionContract(s), null, 'a preview has no self-completing beat')
})

test('drag arm→cancel with matching id: back to idle single', () => {
  let s = modeReducer(single(), { type: 'drag-arm', now: 0 })
  s = modeReducer(s, { type: 'drag-cancel', id: s.transition.id })
  assertIdle(s, 'single')
})

test('drag arm→cancel with STALE id: ignored (INV 5)', () => {
  let s = modeReducer(single(), { type: 'drag-arm', now: 0 })
  s = modeReducer(s, { type: 'drag-cancel', id: s.transition.id + 99 })
  assert.equal(s.transition.phase, 'drag-preview')
})

test('drag arm→commit: mode flips to panes, preview cleared, one transaction', () => {
  let s = modeReducer(single(), { type: 'drag-arm', now: 0 })
  s = modeReducer(s, { type: 'drag-commit', id: s.transition.id })
  assert.equal(s.committedMode, 'panes')
  assert.equal(s.transition, null, 'no lingering preview or fabricated entry beat')
  assertIdle(s, 'panes')
})

test('drag commit with STALE id: ignored — no fabricated mode change (P1 finding)', () => {
  let s = modeReducer(single(), { type: 'drag-arm', now: 0 })
  s = modeReducer(s, { type: 'drag-commit', id: s.transition.id + 5 })
  assert.equal(s.committedMode, 'single')
  assert.equal(s.transition.phase, 'drag-preview')
})

test('drag arm in BUILDER is not a preview (already tiled)', () => {
  const s = modeReducer(panes(), { type: 'drag-arm', now: 0 })
  assert.equal(s.transition, null)
  assert.equal(s.committedMode, 'panes')
})

test('arm during exit: exit committed single, so the arm previews builder', () => {
  let s = modeReducer(panes(), { type: 'toggle', presentation: exitPlan(), now: 0 })
  const exitId = s.transition.id
  s = modeReducer(s, { type: 'drag-arm', now: 30 })
  assert.equal(s.committedMode, 'single')
  assert.equal(s.transition.phase, 'drag-preview')
  assert.notEqual(s.transition.id, exitId)
  assert.equal(exitBeatActive(s), false, 'no stranded exit beat')
})

// ── Auto-return and Undo ────────────────────────────────────────────────────

test('auto-return toggle (last-tab close) with no plan: instant flip to single', () => {
  const s = modeReducer(panes(), { type: 'toggle', cause: 'auto', to: 'single', presentation: null })
  assert.equal(s.committedMode, 'single')
  assert.equal(s.transition, null, 'an emptied tree has no pane to deal — instant')
})

test('undo restoring panes from single: re-enters builder with the entry deal', () => {
  const s = modeReducer(single(), { type: 'undo', restoredMode: 'panes', presentation: enterPlan(), now: 0 })
  assert.equal(s.committedMode, 'panes')
  assert.equal(s.transition.phase, 'entering')
  assert.equal(s.transition.cause, 'undo')
})

test('ordinary tree undo (mode unchanged): no beat, same committed mode', () => {
  const base = panes()
  const s = modeReducer(base, { type: 'undo', restoredMode: 'panes', presentation: null })
  assert.equal(s, base, 'no-op reference — undo carried the current mode forward')
})

test('undo restoring single from panes: exit deal', () => {
  const s = modeReducer(panes(), { type: 'undo', restoredMode: 'single', presentation: exitPlan(), now: 0 })
  assert.equal(s.committedMode, 'single')
  assert.equal(s.transition.phase, 'exiting')
})

test('later explicit toggle supersedes a coupled undo/auto beat', () => {
  let s = modeReducer(panes(), { type: 'toggle', cause: 'auto', to: 'single', presentation: exitPlan(), now: 0 })
  s = modeReducer(s, { type: 'toggle', presentation: enterPlan(), now: 40 })
  assert.equal(s.committedMode, 'panes')
  assert.equal(s.transition.phase, 'entering')
})

// ── INV 10: invalidation snaps ──────────────────────────────────────────────

test('cancel-beat clears the descriptor without touching committedMode (INV 10)', () => {
  let s = modeReducer(panes(), { type: 'toggle', presentation: exitPlan(), now: 0 })
  s = modeReducer(s, { type: 'cancel-beat' })
  assert.equal(s.transition, null, 'beat cancelled, animation ownership not silently changed')
  assert.equal(s.committedMode, 'single', 'durable mode is unaffected')
  assertIdle(s, 'single')
})

// ── Page lifecycle ──────────────────────────────────────────────────────────

test('sync-committed reconciles an external durable change with no beat (reload policy)', () => {
  let s = modeReducer(single(), { type: 'toggle', presentation: enterPlan(), now: 0 })
  s = modeReducer(s, { type: 'sync-committed', committedMode: 'single' })
  assert.equal(s.committedMode, 'single')
  assert.equal(s.transition, null, 'reload starts in the durable mode with no beat')
})

test('visibility reconcile force-completes a beat that outlived its plan totalMs (INV 14)', () => {
  const plan = exitPlan({ totalMs: 180 })
  const s = modeReducer(panes(), { type: 'toggle', presentation: plan, now: 1000 })
  const late = 1000 + plan.totalMs + RECONCILE_SLACK_MS + 5
  const ev = reconcileVisibleEvent(s, late)
  assert.ok(ev, 'an overdue beat yields a completion event')
  assert.equal(ev.type, 'complete')
  assert.equal(ev.id, s.transition.id)
  assertIdle(modeReducer(s, ev), 'single')
})

test('visibility reconcile leaves a still-fresh beat running', () => {
  const s = modeReducer(panes(), { type: 'toggle', presentation: exitPlan(), now: 1000 })
  assert.equal(reconcileVisibleEvent(s, 1000 + 10), null, 'a fresh beat is not force-completed')
})

// ── INV 7: completion + supersession ────────────────────────────────────────

test('duplicate completion is harmless', () => {
  let s = modeReducer(single(), { type: 'toggle', presentation: enterPlan(), now: 0 })
  const id = s.transition.id
  s = modeReducer(s, { type: 'complete', id })
  assert.equal(modeReducer(s, { type: 'complete', id }), s, 'a second completion is a no-op reference')
})

test('completion from a superseded epoch never clears a newer transition (INV 7)', () => {
  let s = modeReducer(single(), { type: 'toggle', presentation: enterPlan(), now: 0 })
  const oldId = s.transition.id
  s = modeReducer(s, { type: 'toggle', presentation: exitPlan(), now: 50 })
  const newId = s.transition.id
  const after = modeReducer(s, { type: 'complete', id: oldId })
  assert.equal(after.transition.id, newId, 'live transition untouched by the old completion')
})

// ── INV 16: kill-switch clamp ───────────────────────────────────────────────

test('kill switch clamps presentation to single before any derivation', () => {
  const s = panes()
  const opts = { splitsEnabled: false }
  assert.equal(effectiveViewMode(s, opts), 'single', 'a persisted panes blob never reaches tiled render')
  assert.equal(builderModeActive(s, opts), false)
  assert.equal(transitionRootClass(s, opts), '')
  assert.equal(exitBeatActive(s, opts), false)
  assert.equal(dragPreviewActive(s, opts), false)
})

test('kill switch clamps even a live entering transition', () => {
  const s = modeReducer(single(), { type: 'toggle', presentation: enterPlan(), now: 0 })
  assert.equal(effectiveViewMode(s, { splitsEnabled: false }), 'single')
  assert.equal(transitionRootClass(s, { splitsEnabled: false }), '')
})

// ── Unknown events are no-ops ────────────────────────────────────────────────

test('an unknown event is a no-op reference', () => {
  const s = panes()
  assert.equal(modeReducer(s, { type: 'nonsense' }), s)
})
