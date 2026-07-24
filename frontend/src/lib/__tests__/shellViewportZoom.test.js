/* The top-level Möbius viewport preserves native accessibility zoom. */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const indexHtml = readFileSync(new URL('../../../index.html', import.meta.url), 'utf8')
const indexCss = readFileSync(new URL('../../index.css', import.meta.url), 'utf8')

test('the shell viewport preserves native pinch zoom', () => {
  const viewport = indexHtml.match(/<meta name="viewport" content="([^"]+)"/)?.[1] || ''
  assert.match(viewport, /width=device-width/)
  assert.match(viewport, /initial-scale=1(?:\.0)?/)
  assert.doesNotMatch(viewport, /maximum-scale/)
  assert.doesNotMatch(viewport, /user-scalable/)
  assert.match(viewport, /viewport-fit=cover/)
  assert.match(viewport, /interactive-widget=resizes-content/)

  const rootTouchRule = indexCss.match(/html,\s*body\s*\{[\s\S]*?\}/)?.[0] || ''
  assert.match(rootTouchRule, /touch-action:\s*manipulation/)
})
