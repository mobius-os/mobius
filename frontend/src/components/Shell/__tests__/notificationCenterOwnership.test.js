/* Notification-center interaction stays below Shell's workspace render boundary. */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const shell = readFileSync(new URL('../Shell.jsx', import.meta.url), 'utf8')
const center = readFileSync(
  new URL('../../NotificationBell/NotificationCenter.jsx', import.meta.url),
  'utf8',
)
const bellCss = readFileSync(
  new URL('../../NotificationBell/NotificationBell.css', import.meta.url),
  'utf8',
)

test('notification toggles are owned below the workspace shell', () => {
  assert.doesNotMatch(shell, /useNotificationCenter/)
  assert.match(
    shell,
    /<NotificationCenter\s+ref=\{notificationCenterActionsRef\}/,
  )
  assert.match(center, /useNotificationCenter\(queryClient\)/)
  assert.match(center, /useImperativeHandle\(eventActionsRef/)
  assert.match(center, /openSearch/)
  assert.doesNotMatch(center, /document\.addEventListener\('keydown'/)
  assert.match(shell, /useShellShortcuts\(shortcutActions\)/)
})

test('closed bell hover styling only applies to precise pointing devices', () => {
  const baseActiveRule = bellCss.match(/\.notification-bell--active\s*\{[^}]*\}/s)?.[0] || ''
  const preciseHoverRule = bellCss.match(
    /@media \(hover: hover\) and \(pointer: fine\)\s*\{[\s\S]*?\.notification-bell:hover\s*\{[^}]*\}[\s\S]*?\}/,
  )?.[0] || ''

  assert.match(baseActiveRule, /background:\s*var\(--surface\)/)
  assert.match(preciseHoverRule, /background:\s*var\(--surface\)/)
  assert.doesNotMatch(
    bellCss.slice(0, bellCss.indexOf('@media (hover: hover)')),
    /\.notification-bell:hover/,
  )
})
