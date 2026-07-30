import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'

const component = readFileSync(new URL('../NotificationsView.jsx', import.meta.url), 'utf8')
const css = readFileSync(new URL('../NotificationsView.css', import.meta.url), 'utf8')

test('linked notification content participates in native text selection', () => {
  const rowRule = css.match(/\.notifications__row--link\s*\{[^}]*\}/s)?.[0] || ''

  assert.match(rowRule, /user-select:\s*text/)
  assert.match(rowRule, /-webkit-user-select:\s*text/)
  assert.match(rowRule, /-webkit-touch-callout:\s*default/)
  assert.match(
    component,
    /onPointerDown=\{\(\) => \{[\s\S]*textSelectionSnapshot\(\)[\s\S]*event\.detail !== 0[\s\S]*pointerSelectionChangedWithin\([\s\S]*event\.currentTarget[\s\S]*\) return[\s\S]*onOpenTarget/,
    'releasing a pointer selection should not also open its notification target',
  )
})
