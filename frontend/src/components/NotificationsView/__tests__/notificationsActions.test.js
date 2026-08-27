/* Notification actions stay lightweight: one-step clear and no redundant close control. */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const component = readFileSync(new URL('../NotificationsView.jsx', import.meta.url), 'utf8')
const css = readFileSync(new URL('../NotificationsView.css', import.meta.url), 'utf8')

test('notification header clears immediately and closes through the bell boundary', () => {
  assert.match(component, /onClick=\{handleClearAll\}/)
  assert.match(component, /await onClearAll\(\)/)
  assert.match(component, /isClearing \? 'Clearing…' : 'Clear all'/)
  assert.doesNotMatch(component, /confirmClear|Confirm clear|Close notifications/)
  assert.doesNotMatch(css, /notifications__clear-actions|notifications__close/)
  assert.match(
    css,
    /@media \(hover: hover\) and \(pointer: fine\)[\s\S]*?\.notifications__clear:hover/,
  )
  const clearRule = css.match(/\.notifications__clear\s*\{([^}]*)\}/)?.[1] ?? ''
  assert.match(clearRule, /color:\s*var\(--text\)/)
})
