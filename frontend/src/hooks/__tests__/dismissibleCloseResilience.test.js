/* Dismissible-surface close resilience (source contract, matching the house
 * pattern for useNavigation.js coverage).
 *
 * A dismissible sentinel backs the chat image viewer: opening pushes one
 * history entry so the OS back gesture closes the viewer. The close BUTTON must
 * not depend on that entry's traversal ever arriving. On a wedged WebKit
 * Navigation store (iOS 18.4+, see navHistory.mirrorCurrentEntry) the mirror
 * write throws InvalidStateError, the traversal reads back untagged, and the
 * phantom guard discards it — the viewer stayed open, and the old pending-close
 * flag then swallowed every later tap, so the X was dead until relaunch (field
 * report 2026-08-01, standalone iOS PWA, screen recording).
 *
 * These are deliberate SOURCE contracts: useNavigation is a hook with live
 * history/navigation side effects and no mountable unit harness (same rationale
 * as drawerCloseResilience.test.js). The behavior itself is covered end-to-end
 * by tests/navigation.spec.mjs with the Navigation store stubbed wedged. */
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const src = readFileSync(new URL('../useNavigation.js', import.meta.url), 'utf8')
const closeHistoryDismiss = src.slice(
  src.indexOf('const closeHistoryDismiss'),
  src.indexOf('const unregisterHistoryDismiss'),
)
const onNavigate = src.slice(
  src.indexOf('function onNavigate'),
  src.indexOf("navigation.addEventListener('navigate'"),
)
const onPopState = src.slice(
  src.indexOf('function onPopState'),
  src.indexOf("window.addEventListener('popstate'"),
)

test('an explicit close dismisses synchronously, never from the traversal', () => {
  // The registration is dropped and the surface dismissed in the SAME call, so
  // a lost traversal cannot leave the viewer open.
  assert.ok(
    closeHistoryDismiss.indexOf('historyDismissalsRef.current.delete(entryId)')
      < closeHistoryDismiss.indexOf('dismissal.onDismiss()'),
    'the registration is consumed before the surface is dismissed',
  )
  assert.match(closeHistoryDismiss, /dismissal\.onDismiss\(\)\s*\n\s*return true/,
    'every close path ends by dismissing the surface')
  // history.back() is bookkeeping for the sentinel, not the close mechanism.
  assert.ok(
    closeHistoryDismiss.indexOf('history.back()')
      < closeHistoryDismiss.indexOf('dismissal.onDismiss()'),
    'the sentinel is consumed before the dismissal, not instead of it',
  )
  assert.match(closeHistoryDismiss, /try \{ history\.back\(\) \} catch/,
    'an unavailable history store still closes the surface')
  // No pending-close flag may gate a second tap: that is exactly what turned one
  // lost traversal into a permanently dead close button.
  assert.doesNotMatch(closeHistoryDismiss, /closing/,
    'no pending-close flag can swallow a later close')
})

test('onNavigate consumes a dismissible source before the phantom guard', () => {
  const dismissible = onNavigate.indexOf("source?.kind === 'dismissible'")
  const phantomGuard = onNavigate.indexOf('if (!isMobiusNavState(destination)) {')
  const canInterceptReturn = onNavigate.indexOf('if (!e.canIntercept) return')
  assert.ok(dismissible > -1, 'dismissible consumption exists in onNavigate')
  assert.ok(dismissible < phantomGuard,
    'a wedged mirror read must not let the phantom guard discard our own sentinel')
  assert.ok(dismissible < canInterceptReturn,
    'a non-interceptable traversal must still consume the sentinel')
  // The committed landing is read from the authoritative classic store when the
  // mirror is unreadable, so the shell never keeps pointing at an entry it left.
  const branch = onNavigate.slice(dismissible, canInterceptReturn)
  assert.match(branch, /isMobiusNavState\(history\.state\) \? history\.state : null/,
    'the classic store is the fallback for an unreadable mirror destination')
  assert.match(branch, /if \(e\.canIntercept\) e\.intercept[\s\S]{0,80}else setTimeout/,
    'interception is optional, consumption is not')
})

test('onPopState consumes a dismissible source before the phantom guard', () => {
  const dismissible = onPopState.indexOf("source?.kind === 'dismissible'")
  const phantomGuard = onPopState.indexOf('if (!isMobiusNavState(destination)) {')
  assert.ok(dismissible > -1, 'dismissible consumption exists in onPopState')
  assert.ok(dismissible < phantomGuard,
    'an untagged landing beneath the sentinel must still consume it')
})
