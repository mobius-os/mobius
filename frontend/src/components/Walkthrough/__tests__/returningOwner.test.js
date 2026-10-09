import test from 'node:test'
import assert from 'node:assert/strict'
import { returningHandle } from '../returningOwner.js'

test('an_owner_who_arrives_with_a_handle_is_returning', () => {
  assert.equal(returningHandle({ handle: 'ada', claimedHere: false, editing: false }), 'ada')
})

test('a_handle_claimed_in_the_guide_is_not_a_returning_owner_even_after_going_back', () => {
  assert.equal(returningHandle({ handle: 'ada', claimedHere: true, editing: false }), null)
})

test('an_owner_without_a_handle_is_never_returning', () => {
  assert.equal(returningHandle({ handle: null, claimedHere: false, editing: false }), null)
  assert.equal(returningHandle({ handle: undefined, claimedHere: false, editing: false }), null)
})

test('the_note_steps_aside_while_the_handle_form_is_open', () => {
  assert.equal(returningHandle({ handle: 'ada', claimedHere: false, editing: true }), null)
})
