/* Notification actions stay lightweight: one-step clear and no redundant close control. */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const component = readFileSync(new URL('../NotificationsView.jsx', import.meta.url), 'utf8')
const css = readFileSync(new URL('../NotificationsView.css', import.meta.url), 'utf8')
const center = readFileSync(
  new URL('../../NotificationBell/NotificationCenter.jsx', import.meta.url),
  'utf8',
)

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

test('a ready shell update is an actionable bell notification, not a banner', () => {
  assert.match(component, /New shell ready\./)
  assert.match(component, /Reload to use the latest interface changes\./)
  assert.match(component, /onClick=\{onUpdateNow\}[\s\S]*Reload shell/)
  assert.match(component, /onClick=\{onUpdateLater\}[\s\S]*Later/)
  assert.match(component, /rows\.length === 0 && !updateAvailable/)
  assert.match(center, /visibleUnreadCount = unreadCount \+ \(/)
  assert.match(center, /updateAvailable=\{updateNoticeActive\}/)
  assert.match(center, /bellRef\.current\?\.focus\(\)/)
  assert.match(css, /\.notifications__update-action\s*\{[\s\S]*?min-height:\s*36px/)
})

test('notification preview stays content-sized until its compact scroll cap', () => {
  const panelRule = css.match(/\.notifications\s*\{([^}]*)\}/)?.[1] ?? ''
  const contentRule = css.match(/\.notifications__content\s*\{([^}]*)\}/)?.[1] ?? ''

  assert.match(panelRule, /max-height:\s*min\([\s\S]*?70dvh/)
  assert.doesNotMatch(panelRule, /(?:^|\n)\s*height:/)
  assert.match(contentRule, /overflow-y:\s*auto/)
  assert.doesNotMatch(contentRule, /(?:^|\n)\s*flex:/)
})
