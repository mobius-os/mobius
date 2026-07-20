import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const indexCss = readFileSync(new URL('../../../index.css', import.meta.url), 'utf8')
const chatCss = readFileSync(new URL('../ChatView.css', import.meta.url), 'utf8')
const chatView = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')

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
  const stopRules = css.match(/\.chat__stop\s*\{[^}]*\}/g) || []
  const stopRule = stopRules.find((rule) => /background:\s*transparent/.test(rule)) || ''
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

test('mobile hold copies immediately without opening an action menu', () => {
  const css = stripComments(chatCss)

  assert.doesNotMatch(css, /\.chat__copy-menu|\.chat__copy-overlay/,
    'instant copy should not render a menu or modal backdrop')
  assert.match(chatView, /void copyMessage\(message, key\)/,
    'the completed hold should copy the message directly')
  assert.match(chatView, /navigator\.vibrate\?\.\(8\)/,
    'successful copy should offer subtle haptic confirmation where supported')
  assert.match(chatView, /event\.pointerType !== 'touch'/,
    'desktop text interaction should stay unchanged')
})

test('web tool activity uses the assistant reading width', () => {
  const css = stripComments(chatCss)
  const desktopRule = css.match(/@media\s*\(min-width:\s*720px\)\s*\{\s*\.chat__tools\s*\{[^}]*\}/)?.[0] || ''

  assert.match(desktopRule, /width:\s*min\(100%,\s*720px\)/,
    'tool activity should grow to the assistant reading measure on web')
})

test('message sources stay inside the assistant row on narrow screens', () => {
  const css = stripComments(chatCss)
  const sourcesRule = css.match(/\.chat__sources\s*\{[^}]*\}/)?.[0] || ''
  const listRule = css.match(/\.chat__sources-list\s*\{[^}]*\}/)?.[0] || ''

  assert.match(sourcesRule, /width:\s*100%/,
    'align-items:flex-start otherwise lets the sources row grow to max-content')
  assert.match(sourcesRule, /max-width:\s*100%/,
    'the source section must not exceed the assistant message')
  assert.match(sourcesRule, /box-sizing:\s*border-box/,
    'section padding must be included in its width, even outside the app reset')
  assert.match(listRule, /min-width:\s*0/,
    'the flex list must be allowed to shrink long source titles')
  assert.match(listRule, /max-width:\s*100%/,
    'the source list must stay within its section')
  assert.match(listRule, /margin:\s*0/,
    'browser list margins must not push source cards out of alignment')
  assert.match(listRule, /padding:\s*0/,
    'browser list indentation must not reduce the source card width')
})

test('primary chat actions leave a brief empty beat before replacement', () => {
  const css = stripComments(chatCss)
  const actionRule = css.match(/\.chat__send,\s*\.chat__steer,\s*\.chat__stop\s*\{[^}]*\}/)?.[0] || ''
  const revealFrames = css.match(/@keyframes\s+chat-action-reveal\s*\{[\s\S]*?\n\}/)?.[0] || ''

  assert.match(actionRule, /animation:\s*chat-action-reveal/,
    'each keyed primary action should run the replacement reveal')
  assert.match(revealFrames, /0%,\s*44%\s*\{\s*opacity:\s*0/,
    'the incoming action should remain hidden at the start')
  assert.match(revealFrames, /100%\s*\{\s*opacity:\s*1/,
    'the incoming action should then appear')
})

test('running activity uses a masked solid-text sweep, not gradient-clipped text', () => {
  const css = stripComments(chatCss)
  const sweepRules = css
    .match(/\.chat__activity-label-sweep\s*\{[^}]*\}/g)
    ?.join('\n') || ''

  assert.match(sweepRules, /mask-image:\s*linear-gradient/,
    'the bright band should be revealed by a moving mask over solid text')
  assert.doesNotMatch(sweepRules, /background-clip:\s*text/,
    'the activity label must not use gradient-clipped text')
  assert.doesNotMatch(sweepRules, /-webkit-text-fill-color:\s*transparent/,
    'the base or sweep text must never depend on transparent text fill')
})

test('queued row actions expose real touch targets and keyboard focus', () => {
  const css = stripComments(chatCss)
  const steerRule = css.match(/\.queued__steer\s*\{[^}]*\}/)?.[0] || ''
  const cancelRule = css.match(/\.queued__cancel\s*\{[^}]*\}/)?.[0] || ''
  const focusRule = css.match(
    /\.queued__steer:focus-visible,\s*\.queued__cancel:focus-visible\s*\{[^}]*\}/,
  )?.[0] || ''

  for (const rule of [steerRule, cancelRule]) {
    assert.match(rule, /width:\s*44px/)
    assert.match(rule, /height:\s*44px/)
  }
  assert.match(focusRule, /outline:\s*2px solid var\(--accent\)/,
    'both icon-only actions need a visible keyboard focus indicator')
})
