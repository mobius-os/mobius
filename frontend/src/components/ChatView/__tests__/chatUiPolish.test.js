import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const indexCss = readFileSync(new URL('../../../index.css', import.meta.url), 'utf8')
const chatCss = readFileSync(new URL('../ChatView.css', import.meta.url), 'utf8')

function stripComments(css) {
  return css.replace(/\/\*[\s\S]*?\*\//g, '')
}

test('theme transition does not animate every descendant or expensive shadows', () => {
  const css = stripComments(indexCss)
  const transitionRules = css
    .match(/:root\.theme-transitioning[\s\S]*?\}/g)
    ?.join('\n') || ''

  assert.doesNotMatch(css, /theme-transitioning\s+\*/,
    'theme toggles must not install a document-wide transition')
  assert.doesNotMatch(transitionRules, /box-shadow/,
    'theme toggles should not animate box-shadow across chat surfaces')
})

test('restored chat rows and tool blocks do not replay entrance animation', () => {
  const css = stripComments(indexCss)

  assert.doesNotMatch(css, /\.chat__msg\s*\{[^}]*animation\s*:/,
    'message rows should stay still when a chat is restored')
  assert.doesNotMatch(css, /\.chat__tool\s*\{[^}]*animation\s*:/,
    'tool blocks should not flicker on streaming/remount updates')
})

test('stop action has no visible circular shell', () => {
  const css = stripComments(chatCss)
  const stopRule = css.match(/\.chat__stop\s*\{[^}]*\}/)?.[0] || ''
  const stopFocusRule = css.match(/\.chat__stop:focus-visible\s*\{[^}]*\}/)?.[0] || ''
  const stopGlyphFocusRule = css.match(/\.chat__stop:focus-visible svg\s*\{[^}]*\}/)?.[0] || ''

  assert.match(stopRule, /background:\s*transparent/,
    'Stop keeps the touch target but removes the visible circular fill')
  assert.match(stopRule, /border-color:\s*transparent/,
    'Stop should not draw a circular border around the square glyph')
  assert.match(stopFocusRule, /outline:\s*none/,
    'Stop must override the circular action-slot focus outline')
  assert.match(stopGlyphFocusRule, /outline:\s*2px solid var\(--accent\)/,
    'Stop keyboard focus should move to the square glyph')
})
