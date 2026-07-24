import test from 'node:test'
import assert from 'node:assert/strict'
import { undoKeyPressed, isEditableTarget } from '../workspaceOnboarding.js'

// ── undo chord (design §3.5) ─────────────────────────────────────────────────

test('undoKeyPressed matches Cmd/Ctrl+Z but not redo or plain z', () => {
  assert.equal(undoKeyPressed({ metaKey: true, key: 'z' }), true)
  assert.equal(undoKeyPressed({ ctrlKey: true, key: 'Z' }), true)
  assert.equal(undoKeyPressed({ metaKey: true, shiftKey: true, key: 'z' }), false) // redo
  assert.equal(undoKeyPressed({ altKey: true, ctrlKey: true, key: 'z' }), false)
  assert.equal(undoKeyPressed({ key: 'z' }), false) // no modifier
  assert.equal(undoKeyPressed({ metaKey: true, key: 'y' }), false)
})

test('isEditableTarget recognizes text-entry surfaces only', () => {
  assert.equal(isEditableTarget({ tagName: 'INPUT' }), true)
  assert.equal(isEditableTarget({ tagName: 'TEXTAREA' }), true)
  assert.equal(isEditableTarget({ tagName: 'SELECT' }), true)
  assert.equal(isEditableTarget({ tagName: 'DIV', isContentEditable: true }), true)
  assert.equal(isEditableTarget({ tagName: 'DIV' }), false)
  assert.equal(isEditableTarget(null), false)
})
