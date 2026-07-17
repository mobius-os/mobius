import test from 'node:test'
import assert from 'node:assert/strict'
import {
  HINT_KEY, coachmarkArmed, coachmarkDismissed, insertWorkspaceStep,
  undoKeyPressed, isEditableTarget,
} from '../workspaceOnboarding.js'

// ── coachmark arming (design §7.2) ───────────────────────────────────────────

test('the coachmark arms only with the flag on, ≥2 tabs, and not dismissed', () => {
  assert.equal(coachmarkArmed({ enabled: true, tabCount: 2, dismissed: false }), true)
  assert.equal(coachmarkArmed({ enabled: true, tabCount: 1, dismissed: false }), false) // needs ≥2
  assert.equal(coachmarkArmed({ enabled: false, tabCount: 5, dismissed: false }), false) // flag off
  assert.equal(coachmarkArmed({ enabled: true, tabCount: 3, dismissed: true }), false) // already dismissed
})

test('coachmarkDismissed reads the flag and treats broken storage as dismissed', () => {
  assert.equal(coachmarkDismissed({ getItem: (k) => (k === HINT_KEY ? '1' : null) }), true)
  assert.equal(coachmarkDismissed({ getItem: () => null }), false)
  assert.equal(coachmarkDismissed({ getItem: () => { throw new Error('blocked') } }), true)
})

// ── walkthrough step insertion (design §7.1) ─────────────────────────────────

test('insertWorkspaceStep places workspace right after customize when enabled', () => {
  assert.deepEqual(
    insertWorkspaceStep(['intro', 'customize', 'install', 'safety-net', 'first-chat'], true),
    ['intro', 'customize', 'workspace', 'install', 'safety-net', 'first-chat'],
  )
  assert.deepEqual(
    insertWorkspaceStep(['intro', 'customize', 'safety-net', 'first-chat'], true),
    ['intro', 'customize', 'workspace', 'safety-net', 'first-chat'],
  )
})

test('insertWorkspaceStep is a no-op when the flag is off', () => {
  const steps = ['intro', 'customize', 'first-chat']
  assert.equal(insertWorkspaceStep(steps, false), steps) // same reference, untouched
})

test('insertWorkspaceStep never double-inserts or mutates the input', () => {
  const steps = ['intro', 'customize', 'workspace', 'first-chat']
  assert.equal(insertWorkspaceStep(steps, true), steps) // already present → unchanged
  const fresh = ['intro', 'customize', 'first-chat']
  insertWorkspaceStep(fresh, true)
  assert.deepEqual(fresh, ['intro', 'customize', 'first-chat']) // input not mutated
})

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
