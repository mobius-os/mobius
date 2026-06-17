import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'

const SOURCE = readFileSync(
  new URL('../../sw.js', import.meta.url),
  'utf8',
)

function shellNavigationDenylist() {
  const match = SOURCE.match(/denylist:\s*\[([\s\S]*?)\n\s*\]/)
  assert.ok(match, 'shell NavigationRoute denylist exists')
  return Function(`"use strict"; return [${match[1]}]`)()
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

test('offline app cache key ignores install intent query', () => {
  assert.match(
    SOURCE,
    /searchParams\.delete\(['"]install['"]\)/,
    'offline cache key strips ?install=1',
  )
})
