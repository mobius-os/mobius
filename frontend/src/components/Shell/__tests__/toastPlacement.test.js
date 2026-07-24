import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const jsx = readFileSync(
  new URL('../../ui/Toast.jsx', import.meta.url),
  'utf8',
)
const css = readFileSync(
  new URL('../../ui/Toast.css', import.meta.url),
  'utf8',
)
const shell = readFileSync(
  new URL('../Shell.jsx', import.meta.url),
  'utf8',
)
const shellCss = readFileSync(
  new URL('../Shell.css', import.meta.url),
  'utf8',
)
const intentNavigation = readFileSync(
  new URL('../useAppIntentNavigation.js', import.meta.url),
  'utf8',
)

test('every dismissible toast exposes an explicit accessible close', () => {
  assert.match(jsx, /aria-label="Dismiss notification"/)
  assert.match(jsx, /onClick=\{handleDismiss\}/)
  assert.match(jsx, /clearTimeout\(timerRef\.current\)/)
})

test('Shell keeps timeout identity stable and remounts repeated notices', () => {
  assert.match(shell, /const dismissToast = useCallback/)
  assert.match(shell, /toastSequenceRef\.current \+= 1/)
  assert.match(shell, /key=\{toast\?\.sequence \|\| 'toast-empty'\}/)
  assert.match(shell, /useAppIntentNavigation\(\{[\s\S]*showToast,/)
  assert.match(intentNavigation, /showToast\('App is not installed yet\.'/)
  assert.doesNotMatch(intentNavigation, /setToast/)
})

test('toast uses shell-owned chrome geometry, not composer or font guesses', () => {
  assert.match(css, /right:\s*max\(1rem,\s*env\(safe-area-inset-right/)
  assert.match(css, /var\(--shell-bar-height,\s*58px\)/)
  assert.match(css, /var\(--shell-tabstrip-height,\s*41px\)/)
  assert.match(shellCss, /--shell-bar-height:\s*58px/)
  assert.match(shellCss, /--shell-tabstrip-height:\s*41px/)
  assert.match(shellCss, /height:\s*calc\(var\(--shell-bar-height\)/)
  assert.match(shellCss, /height:\s*var\(--shell-tabstrip-height\)/)
  assert.match(css, /@media \(max-width: 700px\)/)
  assert.doesNotMatch(css, /@media \(max-width: 700px\)[\s\S]*?top:/)
  assert.doesNotMatch(css, /bottom:/)
})

test('toast actions and dismissal have comfortable pointer targets', () => {
  assert.match(css, /\.toast__action\s*\{[\s\S]*?min-height:\s*36px/)
  assert.match(css, /\.toast__dismiss\s*\{[\s\S]*?width:\s*36px[\s\S]*?height:\s*36px/)
})
