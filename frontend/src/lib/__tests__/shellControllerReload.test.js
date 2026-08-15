import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const indexHtml = readFileSync(new URL('../../../index.html', import.meta.url), 'utf8')
const controllerSource = readFileSync(
  new URL('../../components/Shell/useShellReloadController.js', import.meta.url),
  'utf8',
)
const navigationSource = readFileSync(
  new URL('../../hooks/useNavigation.js', import.meta.url),
  'utf8',
)
const shellSource = readFileSync(
  new URL('../../components/Shell/Shell.jsx', import.meta.url),
  'utf8',
)

test('the boot script discovers workers but never owns a shell reload', () => {
  assert.match(indexHtml, /navigator\.serviceWorker\.register\('\/sw\.js'/)
  assert.match(indexHtml, /recoverStalePrecacheIfNeeded/)
  assert.doesNotMatch(indexHtml, /reloadAfterControllerChange/)
  assert.doesNotMatch(indexHtml, /sw-skip-initiated|sw-auto-reloaded/)
  assert.doesNotMatch(
    indexHtml,
    /navigator\.serviceWorker\.addEventListener\(\s*['"]controllerchange['"]/,
  )
})

test('the shell controller has one deduplicated reload executor', () => {
  assert.match(controllerSource, /if \(performingRef\.current\) return/)
  assert.match(controllerSource, /const reload = \(\) => \{/)
  assert.match(controllerSource, /settleNewestWorkerForHandoff\(\{ registration \}\)/)
  assert.doesNotMatch(controllerSource, /sw-skip-initiated|sw-auto-reloaded/)
})

test('a claimed destination runs before navigation history or view mutation', () => {
  const navTo = navigationSource.indexOf('function navTo(')
  const claim = navigationSource.indexOf(
    'beforeNavigateRef?.current?.(nextRoute)', navTo,
  )
  const epoch = navigationSource.indexOf('navigationEpochRef.current += 1', claim)
  const apply = navigationSource.indexOf('applyModeDestination(nextRoute)', claim)
  assert.ok(
    navTo >= 0 && navTo < claim && claim < epoch && epoch < apply,
    'the reload claim must happen before history and the outgoing document paint',
  )
  assert.doesNotMatch(
    navigationSource.slice(0, navTo),
    /beforeNavigateRef\?\.current\?\.\(nextRoute\)/,
    'render-time route reconciliation has no nextRoute to claim',
  )
})

test('Shell connects navigation to the current reload claimant through one ref', () => {
  assert.match(shellSource, /const beforeNavigateRef = useRef\(null\)/)
  assert.match(shellSource, /beforeNavigateRef,\s*/)
  assert.match(
    shellSource,
    /beforeNavigateRef\.current = claimPendingShellReloadNavigation/,
  )
})
