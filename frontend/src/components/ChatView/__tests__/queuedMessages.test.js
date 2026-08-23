import assert from 'node:assert/strict'
import { test } from 'node:test'

import { restoreQueuedEditorAfterSave } from '../queuedEditorFocus.js'


test('a failed queued-message save returns focus to the active editor', () => {
  const calls = []
  const editor = { focus: options => calls.push(options) }

  assert.equal(restoreQueuedEditorAfterSave('error', editor), true)
  assert.deepEqual(calls, [{ preventScroll: true }])
})


test('a successful queued-message save does not refocus its removed editor', () => {
  let focused = false
  const editor = { focus: () => { focused = true } }

  assert.equal(restoreQueuedEditorAfterSave('saved', editor), false)
  assert.equal(focused, false)
})


test('queued-editor focus falls back for older browsers', () => {
  let calls = 0
  const editor = {
    focus: options => {
      calls += 1
      if (options) throw new TypeError('focus options unsupported')
    },
  }

  assert.equal(restoreQueuedEditorAfterSave('gone', editor), true)
  assert.equal(calls, 2)
})
