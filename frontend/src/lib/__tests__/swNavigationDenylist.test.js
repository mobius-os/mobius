import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'

const SOURCE = readFileSync(
  new URL('../../sw.js', import.meta.url),
  'utf8',
)

// The denylist spreads the legacy instance-local extension point. Keep that
// binding available while also testing the stock reserved /services subtree.
function shellNavigationDenylist(proxied = null) {
  const match = SOURCE.match(/denylist:\s*\[([\s\S]*?)\n\s*\]/)
  assert.ok(match, 'shell NavigationRoute denylist exists')
  let declaration
  if (proxied === null) {
    const constMatch = SOURCE.match(/^const PROXIED_APP_SUBTREES = (\[[\s\S]*?\])/m)
    assert.ok(constMatch, 'PROXIED_APP_SUBTREES extension point exists')
    declaration = constMatch[1]
  } else {
    declaration = `[${proxied}]`
  }
  return Function(
    `"use strict"; const PROXIED_APP_SUBTREES = ${declaration}; return [${match[1]}]`,
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
  assert.equal(denied('/app-embeds/by-id/60/index.html'), true)
  assert.equal(denied('/app-embeds/by-id/60/static/js/main.js'), true)
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

test('guarded local services bypass the shell at every depth', () => {
  const denied = path => shellNavigationDenylist().some(re => re.test(path))

  assert.equal(denied('/services'), true)
  assert.equal(denied('/services/'), true)
  assert.equal(denied('/services/recipes'), true)
  assert.equal(denied('/services/recipes/setup/'), true)
  assert.equal(denied('/services/recipes/accounts/login/'), true)
  assert.equal(denied('/services/recipes/api/recipe/42/'), true)
  // No concrete instance service is compiled into the shell. An old ad-hoc
  // prefix is still an ordinary SPA path unless it moves under /services/.
  assert.equal(denied('/recipes/setup/step/2'), false)
  assert.equal(denied('/shell/chat/abc'), false)
})

test('legacy reverse-proxy extension still ships empty', () => {
  const constMatch = SOURCE.match(/^const PROXIED_APP_SUBTREES = (\[[\s\S]*?\])/m)
  assert.ok(constMatch, 'PROXIED_APP_SUBTREES extension point exists')
  assert.equal(constMatch[1].replace(/\s/g, ''), '[]')
})

test('a configured legacy proxy subtree still bypasses the shell deeply', () => {
  const denylist = shellNavigationDenylist(String.raw`/^\/recipes(\/|$)/`)
  const denied = path => denylist.some(re => re.test(path))

  assert.equal(denied('/recipes'), true)
  assert.equal(denied('/recipes/setup/step/2'), true)
  assert.equal(denied('/other/deep/path'), false)
})

test('offline app cache key ignores install intent query', () => {
  assert.match(
    SOURCE,
    /searchParams\.delete\(['"]install['"]\)/,
    'offline cache key strips ?install=1',
  )
})
