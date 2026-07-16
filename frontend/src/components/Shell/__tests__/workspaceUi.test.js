import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const css = readFileSync(
  new URL('../workspace.css', import.meta.url),
  'utf8',
)

test('the phone pane switcher keeps a 44px touch target', () => {
  const rule = css.match(/\.workspace__pane-chip\s*\{[\s\S]*?\}/)?.[0] || ''
  assert.match(rule, /min-height:\s*44px/)
})

test('the workspace menu avoids an oversized border-and-shadow card', () => {
  const rule = css.match(/\.workspace__menu\s*\{[\s\S]*?\}/)?.[0] || ''
  assert.match(rule, /border:\s*1px/)
  assert.match(rule, /box-shadow:\s*0 4px 8px/)
  assert.doesNotMatch(rule, /box-shadow:[^;]*(?:1[6-9]|[2-9]\d)px/)
})
