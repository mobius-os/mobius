import { test } from 'node:test'
import assert from 'node:assert/strict'
import { holdInert, releaseInert } from '../useDialogFocus.js'

test('an_element_held_by_two_dialogs_stays_inert_until_both_let_go', () => {
  const element = { inert: false }
  holdInert(element)
  holdInert(element)
  releaseInert(element)
  assert.equal(element.inert, true)
  releaseInert(element)
  assert.equal(element.inert, false)
})

test('an_element_that_was_already_inert_is_left_inert', () => {
  const element = { inert: true }
  holdInert(element)
  releaseInert(element)
  assert.equal(element.inert, true)
})

// A shell view stays locked while its view is hidden. If it is shown again while
// a dialog elsewhere still holds it, closing that dialog must not re-lock it.
test('a_view_shown_while_held_stays_usable_after_the_dialog_closes', () => {
  const view = { inert: true }
  holdInert(view)
  view.inert = false
  releaseInert(view)
  assert.equal(view.inert, false)
})

test('a_view_shown_while_held_stays_usable_after_nested_dialogs_close', () => {
  const view = { inert: true }
  holdInert(view)
  view.inert = false
  holdInert(view)
  releaseInert(view)
  assert.equal(view.inert, true)
  releaseInert(view)
  assert.equal(view.inert, false)
})

test('a_view_unlocked_by_its_owner_stays_blocked_while_another_dialog_holds_it', () => {
  const view = { inert: true }
  holdInert(view)
  holdInert(view)
  view.inert = false
  releaseInert(view)
  assert.equal(view.inert, true)
  releaseInert(view)
  assert.equal(view.inert, false)
})
