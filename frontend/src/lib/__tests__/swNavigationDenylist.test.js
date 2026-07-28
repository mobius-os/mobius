import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'

import {
  isShellNavigationDenied,
  PROXIED_APP_SUBTREES,
} from '../swNavigationPolicy.js'

const SOURCE = readFileSync(
  new URL('../../sw.js', import.meta.url),
  'utf8',
)

test('shell route and offline fallback consume one navigation policy', () => {
  assert.match(
    SOURCE,
    /request\.mode === 'navigate' && !isShellNavigationDenied\(url\.pathname\)/,
  )
  assert.match(
    SOURCE,
    /request\.mode === 'navigate' && isShellNavigationDenied\(url\.pathname\),\s+new NetworkOnly\(\)/,
  )
  assert.match(SOURCE, /if \(isShellNavigationDenied\(url\.pathname\)\)/)
  assert.doesNotMatch(SOURCE, /new NavigationRoute|NavigationRoute,/)
})

test('shell app navigation does not intercept top-level app-like routes', () => {
  const denied = isShellNavigationDenied

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
  const denied = isShellNavigationDenied

  assert.equal(denied('/shell/embed/chat'), true)
  assert.equal(denied('/shell/embed'), true)
  // The full shell still serves from the precache — only the embed subtree
  // is excluded, NOT every /shell/ route.
  assert.equal(denied('/shell/'), false)
  assert.equal(denied('/shell/chat/abc'), false)
})

test('guarded local services bypass the shell at every depth', () => {
  const denied = isShellNavigationDenied

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
  assert.deepEqual(PROXIED_APP_SUBTREES, [])
})

test('server-owned and standalone navigations never catch-fallback to shell', () => {
  const denied = value => {
    const url = new URL(value, 'https://mobius.example')
    return isShellNavigationDenied(url.pathname)
  }

  for (const path of [
    '/api/health',
    '/api/chats/demo',
    '/api?source=browser',
    '/recover',
    '/recover/chat',
    '/recover?source=notification',
    '/shell/embed',
    '/shell/embed/chat',
    '/shell/embed?chat=demo',
    '/sites/demo/index.html',
    '/sites?published=demo',
    '/services/recipes/accounts/login/',
    '/services?app=recipes',
    '/apps/cuberun/',
    '/app-assets/cuberun/index.html',
    '/app-embeds/by-id/60/index.html',
    '/cuberun?install=1',
  ]) {
    assert.equal(denied(path), true, `${path} stays server/app owned`)
  }
  for (const path of ['/', '/shell/', '/shell/chat/abc']) {
    assert.equal(denied(path), false, `${path} remains an offline shell route`)
  }
})

test('offline app cache key ignores install intent query', () => {
  assert.match(
    SOURCE,
    /searchParams\.delete\(['"]install['"]\)/,
    'offline cache key strips ?install=1',
  )
})
