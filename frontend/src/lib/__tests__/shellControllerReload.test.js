import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const indexHtml = readFileSync(new URL('../../../index.html', import.meta.url), 'utf8')

test('controller-change reload preserves Shell live-route snapshot', () => {
  const reloadHandler = indexHtml.slice(
    indexHtml.indexOf('const reloadAfterControllerChange'),
    indexHtml.indexOf("navigator.serviceWorker.addEventListener(\n          'controllerchange'"),
  )

  assert.match(reloadHandler, /if \(!sessionStorage\.getItem\('shell-reload'\)\) \{\s*rememberShellStateForAutoReload\(\)\s*\}/)
  assert.ok(
    reloadHandler.indexOf("sessionStorage.getItem('shell-reload')")
      < reloadHandler.indexOf('rememberShellStateForAutoReload()'),
    'the live Shell snapshot must be checked before the localStorage fallback writes',
  )
})
