/* The top-level Möbius viewport stays fixed while local content owns any zoom. */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const indexHtml = readFileSync(new URL('../../../index.html', import.meta.url), 'utf8')
const indexCss = readFileSync(new URL('../../index.css', import.meta.url), 'utf8')

test('the shell viewport cannot scale its toolbar and drawer with page zoom', () => {
  const viewport = indexHtml.match(/<meta name="viewport" content="([^"]+)"/)?.[1] || ''
  assert.match(viewport, /width=device-width/)
  assert.match(viewport, /initial-scale=1(?:\.0)?/)
  assert.match(viewport, /maximum-scale=1/)
  assert.match(viewport, /user-scalable=no/)
  assert.match(viewport, /viewport-fit=cover/)
  assert.match(viewport, /interactive-widget=resizes-content/)

  const rootTouchRule = indexCss.match(/html,\s*body\s*\{[\s\S]*?\}/)?.[0] || ''
  assert.match(rootTouchRule, /touch-action:\s*pan-x pan-y/)
  assert.doesNotMatch(rootTouchRule, /pinch-zoom|manipulation/)
})
