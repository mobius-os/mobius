/* The Apps strip's one-shot click guard: a touch hold that opened a menu (or
   a drag that scrolled the row) swallows only its own follow-up click, never
   a later deliberate tap or key press. */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createHeldClickGuard } from '../../Drawer/heldClickGuard.js'

// Mirrors the strip's wiring: the row retires the guard on pointerdown and
// keydown (capture), a hold or drag claims it, and a tile click consumes it.
function strip() {
  const guard = createHeldClickGuard()
  const opened = []
  return {
    opened,
    pointerDown: () => guard.retire(),
    keyDown: () => guard.retire(),
    holdOpensMenu: () => guard.claim(),
    dragEnds: () => guard.claim(),
    click: app => { if (!guard.consume()) opened.push(app) },
  }
}

test('the click that ends a menu-opening hold does not also open the app', () => {
  const s = strip()
  s.pointerDown()
  s.holdOpensMenu()
  s.click('notes')
  assert.deepEqual(s.opened, [])

  s.pointerDown()
  s.click('notes')
  assert.deepEqual(s.opened, ['notes'])
})

test('after a hold ends in pointercancel, the next tap on any app opens it', () => {
  const s = strip()
  s.pointerDown()
  s.holdOpensMenu()
  // pointercancel: the browser sends no click for the hold.
  s.pointerDown()
  s.click('calendar')
  assert.deepEqual(s.opened, ['calendar'])
})

test('after a hold ends in pointercancel, keyboard activation opens the app', () => {
  const s = strip()
  s.pointerDown()
  s.holdOpensMenu()
  s.keyDown()
  s.click('calendar')
  assert.deepEqual(s.opened, ['calendar'])
})

test('a drag that scrolled the row swallows only its own release click', () => {
  const s = strip()
  s.pointerDown()
  s.dragEnds()
  s.click('notes')
  s.pointerDown()
  s.click('notes')
  assert.deepEqual(s.opened, ['notes'])
})
