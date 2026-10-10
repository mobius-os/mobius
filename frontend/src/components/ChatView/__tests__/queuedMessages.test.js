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


test('a queued preview never cuts an emoji in half', async () => {
  const { queuedPreview } = await import('../queuedPreview.js')
  const text = `a${'😀'.repeat(40)}`
  const { preview, needsTruncation } = queuedPreview(text)

  assert.equal(needsTruncation, true)
  assert.ok(preview.endsWith('😀…'), preview)
  assert.equal(preview.isWellFormed(), true)
})


test('a queued preview keeps short and multi-line text readable', async () => {
  const { queuedPreview } = await import('../queuedPreview.js')

  assert.deepEqual(queuedPreview('hi'), { preview: 'hi', needsTruncation: false })
  assert.deepEqual(
    queuedPreview('first\nsecond'),
    { preview: 'first …', needsTruncation: true },
  )
  assert.equal(queuedPreview('x'.repeat(81)).preview, `${'x'.repeat(80)}…`)
})
