import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  initialModeState, modeReducer,
  effectiveViewMode, builderModeActive, dragPreviewActive,
} from '../modeMachine.js'

function single() { return initialModeState('single') }
function panes() { return initialModeState('panes') }

test('stable modes present their committed world', () => {
  assert.equal(effectiveViewMode(single()), 'single')
  assert.equal(builderModeActive(single()), false)
  assert.equal(effectiveViewMode(panes()), 'panes')
  assert.equal(builderModeActive(panes()), true)
})

test('the actual durable workspace transition synchronizes the presented world', () => {
  const entered = modeReducer(single(), { type: 'sync-committed', committedMode: 'panes' })
  assert.equal(entered.committedMode, 'panes')
  assert.equal(entered.transition, null)
  const exited = modeReducer(entered, { type: 'sync-committed', committedMode: 'single' })
  assert.equal(exited.committedMode, 'single')
  assert.equal(exited.transition, null)
})

test('synchronizing the resting durable mode is a no-op reference', () => {
  const state = panes()
  assert.equal(modeReducer(state, { type: 'sync-committed', committedMode: 'panes' }), state)
})

test('drag arm is the only transient projection and exposes Builder from Standard', () => {
  const preview = modeReducer(single(), { type: 'drag-arm' })
  assert.equal(preview.transition.phase, 'drag-preview')
  assert.equal(preview.transition.id, 1)
  assert.equal(preview.committedMode, 'single')
  assert.equal(effectiveViewMode(preview), 'panes')
  assert.equal(dragPreviewActive(preview), true)
})

test('only the matching drag epoch can cancel a preview', () => {
  const preview = modeReducer(single(), { type: 'drag-arm' })
  assert.equal(modeReducer(preview, { type: 'drag-cancel', id: 99 }), preview)
  const cancelled = modeReducer(preview, { type: 'drag-cancel', id: 1 })
  assert.equal(cancelled.transition, null)
  assert.equal(cancelled.committedMode, 'single')
})

test('ending a drag cannot predict or commit the durable workspace mode', () => {
  const preview = modeReducer(single(), { type: 'drag-arm' })
  const attempted = modeReducer(preview, { type: 'drag-commit', id: 1 })
  assert.equal(attempted, preview, 'only sync-committed may change durable mode')
  const ended = modeReducer(preview, { type: 'drag-cancel', id: 1 })
  assert.equal(ended.committedMode, 'single')
  assert.equal(ended.transition, null)
})

test('drag arm is inert from Builder and clears impossible stale preview state', () => {
  const state = panes()
  assert.equal(modeReducer(state, { type: 'drag-arm' }), state)
})

test('external durable-mode synchronization clears transient projection', () => {
  const preview = modeReducer(single(), { type: 'drag-arm' })
  const synced = modeReducer(preview, { type: 'sync-committed', committedMode: 'single' })
  assert.equal(synced.transition, null)
  assert.equal(synced.committedMode, 'single')
})
