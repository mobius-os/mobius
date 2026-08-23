import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  isInlineEditorSubmit,
  isPlainTextPasteShortcut,
  resolveComposerEnterAction,
} from '../composerShortcuts.js'

const enter = (overrides = {}) => ({ key: 'Enter', ...overrides })

test('Cmd+Enter steers composer text when a live turn can accept it', () => {
  assert.equal(
    resolveComposerEnterAction(enter({ metaKey: true }), {
      hasInput: true,
      canSteer: true,
      canSubmitSteer: true,
      isTouchPrimary: false,
    }),
    'submit-steer',
  )
})

test('Ctrl+Enter steers composer text when a live turn can accept it', () => {
  assert.equal(
    resolveComposerEnterAction(enter({ ctrlKey: true }), {
      hasInput: true,
      canSteer: false,
      canSubmitSteer: true,
      isTouchPrimary: false,
    }),
    'submit-steer',
  )
})

test('Cmd/Ctrl+Enter submits normally when there is no steerable live turn', () => {
  assert.equal(
    resolveComposerEnterAction(enter({ metaKey: true }), {
      hasInput: true,
      canSubmitSteer: false,
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

test('inline editor: plain Enter sends on desktop, matching the composer', () => {
  assert.equal(isInlineEditorSubmit(enter(), { isTouchPrimary: false }), true)
})

test('inline editor: plain Enter stays a newline on touch', () => {
  assert.equal(isInlineEditorSubmit(enter(), { isTouchPrimary: true }), false)
})

test('inline editor: Shift+Enter is always a newline', () => {
  assert.equal(isInlineEditorSubmit(enter({ shiftKey: true }), { isTouchPrimary: false }), false)
})

test('inline editor: Cmd/Ctrl+Enter always sends, even on touch', () => {
  assert.equal(isInlineEditorSubmit(enter({ metaKey: true }), { isTouchPrimary: true }), true)
  assert.equal(isInlineEditorSubmit(enter({ ctrlKey: true }), { isTouchPrimary: false }), true)
})

test('inline editor: non-Enter keys never send', () => {
  assert.equal(isInlineEditorSubmit({ key: 'a' }, { isTouchPrimary: false }), false)
})

test('Cmd/Ctrl+Shift+V requests paste without Markdown formatting', () => {
  assert.equal(isPlainTextPasteShortcut({ key: 'v', metaKey: true, shiftKey: true }), true)
  assert.equal(isPlainTextPasteShortcut({ key: 'V', ctrlKey: true, shiftKey: true }), true)
  assert.equal(isPlainTextPasteShortcut({ key: 'v', metaKey: true, shiftKey: false }), false)
  assert.equal(isPlainTextPasteShortcut({ key: 'v', shiftKey: true }), false)
})
