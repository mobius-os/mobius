/* Drawer-close resilience against a wedged WebKit Navigation API (source
 * contract, matching the house pattern for useNavigation.js coverage).
 *
 * The Navigation store is a best-effort MIRROR of the classic History store
 * (navHistory.mirrorCurrentEntry). On a wedged WebKit engine (iOS 18.4+) the
 * mirror is unreadable AND unwritable: updateCurrentEntry throws
 * InvalidStateError, and getState() returns undefined for entries whose
 * mirror write failed. Observed in the field 2026-07-29 (standalone iOS PWA):
 * the drawer opened once per launch, a scrim-close walked the session history
 * backwards out of the shell via the phantom guard, and every later open was
 * silently refused until relaunch.
 *
 * These are deliberate SOURCE contracts: useNavigation is a hook with live
 * history/navigation side effects that has no mountable unit harness, and the
 * wedged-engine behavior was verified against the rendered shell with the
 * Navigation API stubbed to throw/return-undefined. The contracts lock the
 * three load-bearing shapes of that fix. */
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const src = readFileSync(new URL('../useNavigation.js', import.meta.url), 'utf8')
const onNavigate = src.slice(src.indexOf('function onNavigate'), src.indexOf("navigation.addEventListener('navigate'"))

test('onNavigate consumes a pending drawer close BEFORE the phantom guard', () => {
  const pendingClose = onNavigate.indexOf('drawerClosePendingRef.current && source?.kind === \'drawer\'')
  const phantomGuard = onNavigate.indexOf('!isMobiusNavState(destination)')
  assert.ok(pendingClose > -1, 'pending-close consumption exists in onNavigate')
  assert.ok(phantomGuard > -1, 'phantom guard exists in onNavigate')
  // A wedged mirror store makes the destination read untagged; if the phantom
  // guard ran first it would misread our own close as an iframe phantom,
  // re-issue history.back() per traversal, and never clear the pending flag.
  assert.ok(pendingClose < phantomGuard,
    'pending drawer close must be consumed before the phantom guard can misread it')
  // And it must not depend on interception being available: canIntercept may
  // be false for traversals on some engines; the close still consumes.
  const canInterceptReturn = onNavigate.indexOf('if (!e.canIntercept) return')
  assert.ok(canInterceptReturn > pendingClose,
    'pending-close consumption must precede the canIntercept early-return')
})

test('a pending close landing on a classic-store phantom keeps seeking, not finishing', () => {
  // The wedged-mirror recovery must not swallow the HEALTHY-engine phantom
  // seek: when the committed entry is untagged in the authoritative classic
  // store too, the landing is a genuine iframe phantom beneath the sentinel —
  // the close's tagged home is deeper. Finishing there would clear the pending
  // flags and strand the shell on the untagged entry (caught by e2e
  // navigation 31, "seeks through phantom history").
  const pendingClose = onNavigate.indexOf('const pendingDrawerClose')
  const consumeStart = onNavigate.indexOf('const consume = () =>', pendingClose)
  const consume = onNavigate.slice(
    consumeStart,
    onNavigate.indexOf('// Intercept when the engine allows it', consumeStart),
  )
  assert.match(consume,
    /if \(!committed && drawerClosePendingRef\.current\) \{\s*\n\s*continueDrawerCloseAfterPhantom\(\)\s*\n\s*return/,
    'an untagged classic-store landing during a pending close continues the seek')
  // And the finish path still runs only after that guard.
  assert.ok(consume.indexOf('continueDrawerCloseAfterPhantom()') < consume.indexOf('handleBack(committed, source)'),
    'the phantom-seek continuation is decided before the close is finished')
})

test('every Navigation-store read in onNavigate is defensive', () => {
  // The wedged engine throws from its entry accessors just as it does from
  // updateCurrentEntry. Each mirror read must be try/catch-wrapped so a
  // throwing accessor cannot kill traversal handling.
  for (const read of [
    'e.destination.getState()',
    'navigation.currentEntry',
    'sourceEntry?.getState?.()',
    'sourceEntry?.index',
    'e.destination?.index',
  ]) {
    const at = onNavigate.indexOf(read)
    assert.ok(at > -1, `${read} is read in onNavigate`)
    const before = onNavigate.slice(Math.max(0, at - 120), at)
    assert.ok(/try\s*{[^}]*$/.test(before), `${read} is wrapped in try/catch`)
  }
})

test('openDrawer reconciles a provably-stale pending close from the classic store', () => {
  const openDrawer = src.slice(src.indexOf('function openDrawer'), src.indexOf('function closeDrawer'))
  // Bounded: within the grace window the original serialization guarantee
  // holds (never re-adopt while the traversal may still land).
  assert.ok(openDrawer.includes('DRAWER_CLOSE_TRAVERSAL_GRACE_MS'),
    'stale-close recovery is gated on the traversal grace window')
  // Re-adopt only on classic-store evidence that the back() never committed:
  // the sentinel is still the CURRENT entry.
  assert.ok(/isMobiusNavState\(history\.state\) && history\.state\.kind === 'drawer'/.test(openDrawer),
    're-adoption requires the classic store to still show the drawer sentinel')
  // closeDrawer stamps the clock the recovery reads.
  const closeDrawer = src.slice(src.indexOf('function closeDrawer'), src.indexOf('const appNavPush'))
  assert.ok(closeDrawer.includes('drawerClosePendingAtRef.current = Date.now()'),
    'closeDrawer stamps the pending-close start time')
})

test('a recovered close settles only after its delayed traversal commits', () => {
  const recovered = src.slice(
    src.indexOf('function recoveredDrawerCloseFor'),
    src.indexOf('function handleBack'),
  )
  assert.match(recovered,
    /closeTraversal\.entryId === navEntryId\(source\)/,
    'the delayed close is correlated by the retagged sentinel identity')
  assert.match(recovered,
    /const committed = isMobiusNavState\(destination\)[\s\S]*history\.state/,
    'settlement reads the committed classic cursor when the Navigation mirror is unavailable')
  assert.match(recovered,
    /pushShellEntry\('nav', closeTraversal\.selectedRoute\)/,
    'a late close rebases the already-painted selection above the committed cursor')

  assert.match(onNavigate,
    /if \(!userInitiated && recoveredDrawerCloseFor\(recoveredSource\)\) \{[\s\S]*e\.intercept\(\{ handler: consumeRecoveredClose \}\)/,
    'programmatic recovery is intercepted while an owner Back remains owner-initiated')
  const recoveredGate = onNavigate.indexOf('recoveredDrawerCloseFor(recoveredSource)')
  const pendingClose = onNavigate.indexOf('const pendingDrawerClose')
  assert.ok(recoveredGate > -1 && recoveredGate < pendingClose,
    'late-close recovery runs before ordinary drawer and phantom traversal handling')
})
