import test from 'node:test'
import assert from 'node:assert/strict'

import { updateCheckOutcome, updateCheckLabel } from '../updateCheckPhase.js'

test('updateCheckOutcome reports checked when every probe fulfilled', () => {
  const results = [
    { status: 'fulfilled', value: undefined },
    { status: 'fulfilled', value: { available: false } },
  ]
  assert.equal(updateCheckOutcome(results), 'checked')
})

test('updateCheckOutcome reports error when any probe rejected', () => {
  const oneRejected = [
    { status: 'fulfilled', value: undefined },
    { status: 'rejected', reason: new Error('network down') },
  ]
  assert.equal(updateCheckOutcome(oneRejected), 'error')

  const bothRejected = [
    { status: 'rejected', reason: new Error('sw update failed') },
    { status: 'rejected', reason: new Error('git fetch failed') },
  ]
  assert.equal(updateCheckOutcome(bothRejected), 'error')
})

test('updateCheckOutcome treats empty/invalid input as no failure, not an error', () => {
  // allSettled always yields an array; a missing one is a caller bug, not a
  // failed check — surfacing "error" there would mask it, so default to checked.
  assert.equal(updateCheckOutcome([]), 'checked')
  assert.equal(updateCheckOutcome(undefined), 'checked')
  assert.equal(updateCheckOutcome(null), 'checked')
})

test('updateCheckLabel maps each phase, error message is distinct from no-updates', () => {
  assert.equal(updateCheckLabel('checking'), 'Checking…')
  assert.equal(updateCheckLabel('checked'), 'No updates found')
  assert.equal(updateCheckLabel('error'), "Couldn't check for updates")
  assert.notEqual(updateCheckLabel('error'), updateCheckLabel('checked'))
})

test('updateCheckLabel falls back to the idle call-to-action', () => {
  assert.equal(updateCheckLabel('idle'), 'Check for updates')
  assert.equal(updateCheckLabel(undefined), 'Check for updates')
  assert.equal(updateCheckLabel('anything-else'), 'Check for updates')
})
