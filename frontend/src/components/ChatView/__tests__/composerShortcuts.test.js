import { test } from 'node:test'
import assert from 'node:assert/strict'

import { resolveComposerEnterAction } from '../composerShortcuts.js'

const enter = (overrides = {}) => ({ key: 'Enter', ...overrides })

test('Cmd+Enter submits composer text', () => {
  assert.equal(
    resolveComposerEnterAction(enter({ metaKey: true }), {
      hasInput: true,
      canSteer: true,
      isTouchPrimary: false,
    }),
    'submit',
  )
})

test('Ctrl+Enter submits composer text', () => {
  assert.equal(
    resolveComposerEnterAction(enter({ ctrlKey: true }), {
      hasInput: true,
      canSteer: false,
      isTouchPrimary: false,
    }),
    'submit',
  )
})

test('Cmd/Ctrl+Enter steers when the composer is empty and steering is available', () => {
  assert.equal(
    resolveComposerEnterAction(enter({ metaKey: true }), {
      hasInput: false,
      canSteer: true,
      isTouchPrimary: false,
    }),
    'steer',
  )
  assert.equal(
    resolveComposerEnterAction(enter({ ctrlKey: true }), {
      hasInput: false,
      canSteer: true,
      isTouchPrimary: true,
    }),
    'steer',
  )
})

test('Cmd/Ctrl+Enter can request steer before the visible fast-forward gate is ready', () => {
  assert.equal(
    resolveComposerEnterAction(enter({ metaKey: true }), {
      hasInput: false,
      canSteer: false,
      canRequestSteer: true,
      isTouchPrimary: false,
    }),
    'steer',
  )
})

test('Cmd/Ctrl+Enter with no text and no steer affordance consumes the shortcut without acting', () => {
  assert.equal(
    resolveComposerEnterAction(enter({ metaKey: true }), {
      hasInput: false,
      canSteer: false,
      isTouchPrimary: false,
    }),
    'noop',
  )
})

test('plain Enter submits composer text on desktop', () => {
  assert.equal(
    resolveComposerEnterAction(enter(), {
      hasInput: true,
      isTouchPrimary: false,
    }),
    'submit',
  )
})

test('plain Enter steers queued text on desktop when the composer is empty', () => {
  assert.equal(
    resolveComposerEnterAction(enter(), {
      hasInput: false,
      canRequestSteer: true,
      isTouchPrimary: false,
    }),
    'steer',
  )
})

test('plain Enter remains a newline on touch-primary devices', () => {
  assert.equal(
    resolveComposerEnterAction(enter(), {
      hasInput: true,
      canRequestSteer: true,
      isTouchPrimary: true,
    }),
    null,
  )
})

test('Shift+Enter always stays a newline chord', () => {
  assert.equal(
    resolveComposerEnterAction(enter({ shiftKey: true, metaKey: true }), {
      hasInput: true,
      canSteer: true,
      isTouchPrimary: false,
    }),
    null,
  )
})

test('non-Enter keys do not trigger composer shortcuts', () => {
  assert.equal(
    resolveComposerEnterAction({ key: 'a', metaKey: true }, {
      hasInput: true,
      canSteer: true,
      isTouchPrimary: false,
    }),
    null,
  )
})
