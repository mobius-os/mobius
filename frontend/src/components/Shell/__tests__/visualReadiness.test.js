import test from 'node:test'
import assert from 'node:assert/strict'

import {
  WORKSPACE_VISUAL_SETTLED,
  WORKSPACE_VISUAL_TRANSITIONING,
  deriveWorkspaceVisualState,
} from '../visualReadiness.js'

const active = world => ({ world, role: 'active' })
const held = world => ({ world, role: 'held' })
const staging = world => ({ world, role: 'staging' })

test('a workspace mode transition owns visual readiness', () => {
  assert.equal(deriveWorkspaceVisualState({
    modeTransition: { id: 4, phase: 'entering' },
    chatPanesVisible: true,
    chatPaneLayers: [active('builder')],
    paintedChatWorld: 'builder',
  }), WORKSPACE_VISUAL_TRANSITIONING)
})

test('a handoff in the painted chat world keeps the shell transitioning', () => {
  assert.equal(deriveWorkspaceVisualState({
    modeTransition: null,
    chatPanesVisible: true,
    chatPaneLayers: [held('builder'), staging('builder'), active('standard')],
    paintedChatWorld: 'builder',
  }), WORKSPACE_VISUAL_TRANSITIONING)
})

test('a retained hidden-world handoff does not block the painted world', () => {
  assert.equal(deriveWorkspaceVisualState({
    modeTransition: null,
    chatPanesVisible: true,
    chatPaneLayers: [active('builder'), held('standard'), staging('standard')],
    paintedChatWorld: 'builder',
  }), WORKSPACE_VISUAL_SETTLED)
})

test('a takeover with chat panes hidden is visually settled', () => {
  assert.equal(deriveWorkspaceVisualState({
    modeTransition: null,
    chatPanesVisible: false,
    chatPaneLayers: [held('builder'), staging('builder')],
    paintedChatWorld: 'builder',
  }), WORKSPACE_VISUAL_SETTLED)
})
