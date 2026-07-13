import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'

const SOURCE = readFileSync(
  new URL('../../sw.js', import.meta.url),
  'utf8',
)

// The denylist literal spreads `PROXIED_APP_SUBTREES` (the reverse-proxied-app
// extension point), so the eval needs that binding in scope. `proxied` lets a
// test drive the extension point without editing the shipped shell.
function shellNavigationDenylist(proxied = null) {
  const match = SOURCE.match(/denylist:\s*\[([\s\S]*?)\n\s*\]/)
  assert.ok(match, 'shell NavigationRoute denylist exists')
  let decl
  if (proxied === null) {
    const constMatch = SOURCE.match(/^const PROXIED_APP_SUBTREES = (\[[\s\S]*?\])/m)
    assert.ok(constMatch, 'PROXIED_APP_SUBTREES extension point exists')
    decl = constMatch[1]
  } else {
    decl = `[${proxied}]`
  }
  return Function(
    `"use strict"; const PROXIED_APP_SUBTREES = ${decl}; return [${match[1]}]`,
  )()
}

test('shell app navigation does not intercept top-level app-like routes', () => {
  const denylist = shellNavigationDenylist()
  const denied = path => denylist.some(re => re.test(path))

  assert.equal(denied('/cuberun'), true)
  assert.equal(denied('/cuberun/'), true)
  assert.equal(denied('/cuberun/index.html'), true)
  assert.equal(denied('/app-assets/cuberun/index.html'), true)
  assert.equal(denied('/app-assets/cuberun/static/js/main.js'), true)
  assert.equal(denied('/klix-filter'), true)
  assert.equal(denied('/cuberunner'), true)
  assert.equal(denied('/shell/'), false)
  assert.equal(denied('/shell/chat/abc'), false)
  assert.equal(denied('/apps/cuberun/'), true)
  assert.equal(denied('/recover/chat'), true)
})

test('shell embed navigation reaches the server, not the non-injected precache', () => {
  // The embed renders OUTSIDE Shell and needs the server-injected theme
  // block on its FIRST paint; the precached index.html omits that block,
  // so /shell/embed/* must be denylisted (mirrors how /recover is handled).
  const denylist = shellNavigationDenylist()
  const denied = path => denylist.some(re => re.test(path))

  assert.equal(denied('/shell/embed/chat'), true)
  assert.equal(denied('/shell/embed'), true)
  // The full shell still serves from the precache — only the embed subtree
  // is excluded, NOT every /shell/ route.
  assert.equal(denied('/shell/'), false)
  assert.equal(denied('/shell/chat/abc'), false)
})

test('reverse-proxied app subtrees ship empty — no instance app baked in', () => {
  // The shipped shell must not deny any concrete instance's proxy prefix; the
  // extension point is empty by default.
  const constMatch = SOURCE.match(/^const PROXIED_APP_SUBTREES = (\[[\s\S]*?\])/m)
  assert.ok(constMatch, 'PROXIED_APP_SUBTREES extension point exists')
  assert.equal(constMatch[1].replace(/\s/g, ''), '[]', 'ships empty')

  // With the empty default, a deep path under an arbitrary would-be proxy
  // prefix still falls through to the SPA (nothing extra is denied).
  const denied = path => shellNavigationDenylist().some(re => re.test(path))
  assert.equal(denied('/recipes/setup/step/2'), false)
})

test('a configured proxied subtree denies the WHOLE subtree, deep paths too', () => {
  // Mechanism/regression guard for the deep-path bounce: when an instance adds
  // its proxied app root, EVERY deep navigation under it must be denied (sent to
  // the network), not just the single-segment root the catch-all already covers.
  const denylist = shellNavigationDenylist(String.raw`/^\/recipes(\/|$)/`)
  const denied = path => denylist.some(re => re.test(path))

  assert.equal(denied('/recipes'), true)
  assert.equal(denied('/recipes/'), true)
  assert.equal(denied('/recipes/setup/'), true)
  assert.equal(denied('/recipes/accounts/login/'), true)
  assert.equal(denied('/recipes/api/recipe/42/'), true)
  // A sibling top-level route is unaffected — still SPA-served.
  assert.equal(denied('/shell/chat/abc'), false)
})

test('offline app cache key ignores install intent query', () => {
  assert.match(
    SOURCE,
    /searchParams\.delete\(['"]install['"]\)/,
    'offline cache key strips ?install=1',
  )
})
