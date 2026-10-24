/* Notification actions stay lightweight and protect irreversible clearing. */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { button, click, findNode, renderHook } from '../../../lib/__tests__/confirmationHarness.mjs'

const { default: NotificationsView } = await import('../NotificationsView.jsx')

const component = readFileSync(new URL('../NotificationsView.jsx', import.meta.url), 'utf8')
const css = readFileSync(new URL('../NotificationsView.css', import.meta.url), 'utf8')
const center = readFileSync(
  new URL('../../NotificationBell/NotificationCenter.jsx', import.meta.url),
  'utf8',
)
const client = readFileSync(new URL('../../../api/client.js', import.meta.url), 'utf8')

test('notification header keeps the irreversible-clear copy and lightweight styling', () => {
  assert.match(component, /Clear history\?/)
  assert.match(component, /Active Undo stays; the rest is deleted/)
  assert.match(component, /isClearing \? 'Clearing…' : 'Clear history'/)
  assert.doesNotMatch(component, /Close notifications/)
  assert.match(css, /notifications__clear-confirm/)
  assert.doesNotMatch(css, /notifications__close/)
  assert.match(
    css,
    /@media \(hover: hover\) and \(pointer: fine\)[\s\S]*?\.notifications__clear:hover/,
  )
  const clearRule = css.match(/\.notifications__clear\s*\{([^}]*)\}/)?.[1] ?? ''
  assert.match(clearRule, /color:\s*var\(--text\)/)
})

function mountNotifications(t, onClearAll) {
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, 'window')
  Object.defineProperty(globalThis, 'window', {
    configurable: true, value: { setInterval: () => 1, clearInterval() {} },
  })
  let view
  t.after(() => {
    view?.unmount()
    if (originalWindow) Object.defineProperty(globalThis, 'window', originalWindow)
    else delete globalThis.window
  })
  view = renderHook(props => NotificationsView(props), { active: true, onClearAll })
  return view
}

test('clear first asks, Keep cancels, and only confirmation deletes once', async t => {
  let clears = 0
  const view = mountNotifications(t, async () => { clears++ })
  click(view, 'Clear all')
  assert.equal(clears, 0, 'opening confirmation must not delete history')
  assert.ok(button(view, 'Clear history'))
  click(view, 'Keep')
  assert.equal(clears, 0, 'cancelling must not delete history')
  assert.ok(button(view, 'Clear all'))
  assert.ok(!findNode(view.result.current, node => node.props?.['aria-label'] === 'Confirm clear notifications'))

  click(view, 'Clear all')
  await click(view, 'Clear history')
  assert.equal(clears, 1)
  assert.ok(button(view, 'Clear all'))
})

test('pending clear disables both choices and its current handler rejects reentry', async t => {
  let clears = 0
  let resolve
  const pending = new Promise(done => { resolve = done })
  const view = mountNotifications(t, () => { clears++; return pending })
  click(view, 'Clear all')
  const request = click(view, 'Clear history')
  assert.equal(clears, 1)
  const working = button(view, 'Clearing…')
  assert.equal(working.disabled, true)
  assert.equal(button(view, 'Keep').disabled, true)
  // Exercise the handler guard too, not merely the browser's disabled control.
  const reentry = working.onClick()
  try {
    assert.equal(clears, 1)
  } finally {
    resolve()
    await Promise.all([request, reentry])
  }
  assert.ok(button(view, 'Clear all'))
})

test('a rejected clear shows an error and leaves confirmation retryable', async t => {
  let clears = 0
  const view = mountNotifications(t, async () => {
    if (++clears === 1) throw new Error('offline')
  })
  click(view, 'Clear all')
  await click(view, 'Clear history')
  const error = findNode(view.result.current, node => node.props?.role === 'alert')
  assert.match(error?.props.children, /Couldn’t clear notifications/)
  assert.equal(button(view, 'Clear history').disabled, false)
  assert.equal(button(view, 'Keep').disabled, false)
  await click(view, 'Clear history')
  assert.equal(clears, 2)
  assert.ok(button(view, 'Clear all'))
  assert.ok(!findNode(view.result.current, node => node.props?.role === 'alert'))
})

test('notifications can be dismissed individually without nesting controls', () => {
  assert.match(component, /onDismiss/)
  assert.match(component, /aria-label=\{`Dismiss \$\{n\.title\}`\}/)
  assert.match(component, /await onDismiss\(notificationId\)/)
  assert.match(center, /onDismiss=\{dismiss\}/)
  assert.match(
    client,
    /dismiss: async \(notificationId\) => jsonOrThrow\([\s\S]*Could not dismiss notification:/,
  )
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
  assert.match(center, /visibleNewCount = newCount \+ \(/)
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
  assert.match(css, /\.notifications__recovery-action\s*\{[\s\S]*?min-height:\s*32px/)
})
