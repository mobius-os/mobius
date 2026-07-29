import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const css = readFileSync(
  fileURLToPath(new URL('../../components/Drawer/Drawer.css', import.meta.url)),
  'utf8',
)

test('chat recency reorders do not move the drawer scroll position', () => {
  const scrollRule = css.match(/\.drawer__scroll\s*\{([^}]*)\}/)?.[1] || ''

  assert.match(
    scrollRule,
    /overflow-anchor:\s*none/,
    'native anchoring must not change scrollTop when a chat row moves to the top',
  )
})
