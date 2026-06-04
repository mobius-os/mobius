import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'

const SOURCE = readFileSync(
  new URL('../../sw.js', import.meta.url),
  'utf8',
)

function shellNavigationDenylist() {
  const match = SOURCE.match(/\{\s*denylist:\s*\[([^\]]+)\]\s*\}/)
  assert.ok(match, 'shell NavigationRoute denylist exists')
  return Function(`"use strict"; return [${match[1]}]`)()
}

test('shell app navigation does not intercept CubeRun root routes', () => {
  const denylist = shellNavigationDenylist()
  const denied = path => denylist.some(re => re.test(path))

  assert.equal(denied('/cuberun'), true)
  assert.equal(denied('/cuberun/'), true)
  assert.equal(denied('/cuberun/index.html'), true)
  assert.equal(denied('/cuberunner'), false)
})
