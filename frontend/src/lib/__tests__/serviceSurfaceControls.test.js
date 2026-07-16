import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const css = readFileSync(
  new URL('../../components/AppCanvas/AppCanvas.css', import.meta.url),
  'utf8',
)

test('service failure controls remain clickable inside the click-through loading cover', () => {
  const loading = css.match(/\.canvas-loading\s*\{([^}]*)\}/)?.[1] || ''
  const offline = css.match(/\.canvas-loading__offline\s*\{([^}]*)\}/)?.[1] || ''

  assert.match(loading, /pointer-events:\s*none/)
  assert.match(offline, /pointer-events:\s*auto/)
})

test('service failure actions render as separated touch-sized buttons', () => {
  const actions = css.match(/\.canvas-loading__offline-actions\s*\{([^}]*)\}/)?.[1] || ''
  const button = css.match(/\.canvas-loading__offline-button\s*\{([^}]*)\}/)?.[1] || ''

  assert.match(actions, /display:\s*flex/)
  assert.match(actions, /gap:\s*10px/)
  assert.match(button, /min-height:\s*44px/)
})
