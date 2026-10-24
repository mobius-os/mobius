import { test } from 'node:test'
import assert from 'node:assert/strict'
import { holdUpload, shouldSuggestSharedFolder } from '../uploadSuggestion.js'

const ready = { status: 'ready', canRemember: true, canPickForVisit: true }
const files = (count, size = 1000) => Array.from({ length: count }, () => ({ size }))
const MB = 1024 * 1024

test('more than 5 files or more than 10 MB suggests sharing the folder', () => {
  assert.equal(shouldSuggestSharedFolder(files(5), ready), false)
  assert.equal(shouldSuggestSharedFolder(files(6), ready), true)
  assert.equal(shouldSuggestSharedFolder(files(1, 10 * MB), ready), false)
  assert.equal(shouldSuggestSharedFolder(files(1, 10 * MB + 1), ready), true)
  assert.equal(shouldSuggestSharedFolder(files(2, 6 * MB), ready), true)
})

test('no suggestion when this browser cannot share a folder now', () => {
  assert.equal(shouldSuggestSharedFolder(files(9), { ...ready, status: 'restart' }), false)
  assert.equal(shouldSuggestSharedFolder(files(9), { ...ready, status: 'unavailable' }), false)
  assert.equal(shouldSuggestSharedFolder(files(9), { ...ready, canRemember: false, canPickForVisit: false }), false)
  assert.equal(shouldSuggestSharedFolder(files(9), { ...ready, canRemember: false }), true)
})

test('the phone app keeps its normal upload until it can share folders', () => {
  globalThis.MobiusNative = {}
  try {
    assert.equal(shouldSuggestSharedFolder(files(9), ready), false)
  } finally {
    delete globalThis.MobiusNative
  }
})

test('files added while the choice is open join the held upload instead of replacing it', () => {
  assert.equal(holdUpload(null, files(2), ready), null)
  const first = holdUpload(null, files(6), ready)
  assert.deepEqual([first.count, first.bytes], [6, 6000])
  const second = holdUpload(first, files(7), ready)
  assert.deepEqual([second.count, second.bytes], [13, 13000])
  const small = holdUpload(second, files(1), ready)
  assert.equal(small.count, 14)
})
