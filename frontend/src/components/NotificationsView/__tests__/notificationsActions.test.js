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

test('ordinary notifications can be dismissed individually without nesting controls', () => {
  assert.match(component, /onDismiss/)
  assert.match(component, /!protectsDismissal && \(/)
  assert.match(component, /aria-label=\{`Dismiss \$\{n\.title\}`\}/)
  assert.match(component, /await onDismiss\(notificationId\)/)
  assert.match(center, /onDismiss=\{dismiss\}/)
  assert.match(
    css,
    /@media \(hover: hover\) and \(pointer: fine\)[\s\S]*?\.notifications__row-shell:hover \.notifications__dismiss/,
  )
  assert.match(css, /\.notifications__dismiss:focus-visible/)
})

test('hover highlights the full notification row while X hover stays local', () => {
  assert.match(css, /\.notifications__row-shell:hover\s*\{\s*background:\s*var\(--surface\)/)
  assert.match(
    css,
    /\.notifications__row-shell:has\(\.notifications__dismiss:hover:not\(:disabled\)\)\s*\{\s*background:\s*transparent/,
  )
  assert.match(css, /\.notifications__row--link:hover\s*\{\s*background:\s*transparent/)
  assert.match(css, /\.notifications__dismiss:hover:not\(:disabled\)\s*\{[^}]*background:\s*var\(--surface\)/)
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

test('durable recovery actions restore in place and preserve their completed receipt', () => {
  assert.match(component, /notificationRecoveryAction\(n\)/)
  assert.match(component, /await onRecoveryAction\(notification\.id, action\)/)
  assert.doesNotMatch(component, /completeAction/)
  assert.match(component, /Restoring…/)
  assert.match(component, /Restored/)
  assert.match(component, /recoveryUnavailableLabel\(recovery, now\)/)
  assert.match(component, /new IntersectionObserver/)
  assert.match(component, /root: contentRef\.current/)
  assert.match(component, /Expires: \{formatDateTime\(recovery\.expiresAt\)\}/)
  assert.match(center, /onRecoveryAction=\{onRecoveryAction\}/)
  assert.match(css, /\.notifications__recovery-action\s*\{[\s\S]*?min-height:\s*44px/)
})
