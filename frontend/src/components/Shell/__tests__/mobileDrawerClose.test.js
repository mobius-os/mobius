import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const drawer = readFileSync(
  new URL('../../Drawer/Drawer.jsx', import.meta.url),
  'utf8',
)
const styles = readFileSync(
  new URL('../../Drawer/Drawer.css', import.meta.url),
  'utf8',
)

test('modal navigation exposes and focuses an explicit close control', () => {
  assert.match(drawer, /aria-label="Close navigation"/)
  assert.match(drawer, /ref=\{closeButtonRef\}/)
  assert.match(drawer, /closeButtonRef\.current\?\.focus\(\)/)
  assert.match(drawer, /disabled=\{interactionLocked\}/)
})

test('the mobile drawer close control keeps a 44px touch target', () => {
  const rule = styles.match(/\.drawer__close\s*\{([^}]*)\}/)?.[1] || ''
  assert.match(rule, /width:\s*44px/)
  assert.match(rule, /height:\s*44px/)
  assert.match(rule, /touch-action:\s*manipulation/)
})

test('close-phase scrim blocking releases only after the drawer is offscreen', () => {
  assert.match(drawer, /new DOMMatrixReadOnly\(getComputedStyle\(e\.currentTarget\)\.transform\)\.m41/)
  assert.match(drawer, /if \(x > -width \+ 1\) return/)
  assert.match(drawer, /setScrimBlocking\(false\)/)
})
