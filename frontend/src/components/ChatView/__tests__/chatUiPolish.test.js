import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const indexCss = readFileSync(new URL('../../../index.css', import.meta.url), 'utf8')
const chatCss = readFileSync(new URL('../ChatView.css', import.meta.url), 'utf8')
const chatView = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')
const chatSettingsPanel = readFileSync(
  new URL('../ChatSettingsPanel.jsx', import.meta.url),
  'utf8',
)

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

test('effort choice stays interactive and fully visible while its optimistic save settles', () => {
  const effortStepper = chatSettingsPanel.match(
    /<EffortStepper\s+efforts=\{rowEfforts\}[\s\S]*?onStopPointerDown=\{preserveFocusUnlessTouch\}[\s\S]*?\/>/,
  )?.[0] || ''

  assert.match(
    effortStepper,
    /disabled=\{switchBusy \|\| !providerConfigured\}/,
    'only a provider switch or unavailable provider should disable effort',
  )
  assert.doesNotMatch(
    effortStepper,
    /disabled=\{[^}]*saving/,
    'a routine save must not dim the effort control into a visible blackout',
  )
  assert.match(
    chatSettingsPanel,
    /if \(reqId !== latestReqId\.current\) return 'stale'/,
    'rapid effort choices remain safe through the existing latest-request guard',
  )
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

test('mobile messages preserve native text selection and its action menu', () => {
  const css = stripComments(chatCss)

  assert.doesNotMatch(css, /\.chat__copy-menu|\.chat__copy-overlay/,
    'messages should not render a custom copy menu or modal backdrop')
  assert.doesNotMatch(chatView, /handleMessagePointerDown|cancelMessageHold|copyMessage/,
    'messages must not intercept the long press used for native text selection')
  assert.doesNotMatch(chatView, /onContextMenu=/,
    'messages must not suppress the native selection action menu')
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

test('Send, Steer, and Stop never fade through an empty replacement frame', () => {
  const css = stripComments(chatCss)
  const sendRule = css.match(/\.chat__send\s*\{[^}]*\}/)?.[0] || ''
  const steerRule = css.match(/\.chat__steer\s*\{[^}]*\}/)?.[0] || ''
  const stopRules = css.match(/\.chat__stop\s*\{[^}]*\}/g)?.join('\n') || ''

  assert.doesNotMatch(sendRule, /animation:/,
    'Send must keep the shared action target continuously visible')
  assert.doesNotMatch(steerRule, /animation:/,
    'Steer must keep the shared action target continuously visible')
  assert.doesNotMatch(stopRules, /animation:/,
    'Stop must appear immediately instead of starting at opacity zero')
  assert.doesNotMatch(css, /@keyframes\s+chat-action-reveal/,
    'the empty-frame reveal must not remain available to a primary action')
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
