import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const indexCss = readFileSync(new URL('../../../index.css', import.meta.url), 'utf8')
const chatCss = readFileSync(new URL('../ChatView.css', import.meta.url), 'utf8')
const chatInputBar = readFileSync(new URL('../ChatInputBar.jsx', import.meta.url), 'utf8')
const chatView = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')
const queuedMessages = readFileSync(new URL('../QueuedMessages.jsx', import.meta.url), 'utf8')
const chatSettingsPanel = readFileSync(
  new URL('../ChatSettingsPanel.jsx', import.meta.url),
  'utf8',
)

function stripComments(css) {
  return css.replace(/\/\*[\s\S]*?\*\//g, '')
}

test('an empty chat keeps the Möbius brand anchor above its prompt', () => {
  assert.match(chatView, /src="\/moebius\.png"/)
  assert.match(chatView, /className="chat__empty-glyph"[\s\S]*?What's on your mind\?/)
  assert.match(chatCss, /\.chat__empty-glyph\s*\{[\s\S]*?width:\s*96px/)
})

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

test('stop action keeps a legible glyph inside its full touch target', () => {
  assert.match(
    chatInputBar,
    /<Stop width=\{28\} height=\{28\} aria-hidden="true" \/>/,
    'the SDK Stop icon needs a 28px box because its square occupies only part of the viewBox',
  )
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

test('message references use a bounded responsive two-column grid', () => {
  const css = stripComments(chatCss)
  const sourcesRule = css.match(/\.chat__sources\s*\{[^}]*\}/)?.[0] || ''
  const listRule = css.match(/\.chat__sources-list\s*\{[^}]*\}/)?.[0] || ''
  const itemRule = css.match(/\.chat__source-item\s*\{[^}]*\}/)?.[0] || ''
  const nonWebItemRule = css.match(/\.chat__source-item:not\(\.chat__source-item--web\)\s*\{[^}]*\}/)?.[0] || ''
  const chipRule = css.match(/\.chat__source-chip\s*\{[^}]*\}/)?.[0] || ''

  assert.match(sourcesRule, /width:\s*100%/,
    'the sources section must fill, but not exceed, the assistant row')
  assert.match(sourcesRule, /max-width:\s*100%/,
    'the source section must not exceed the assistant message')
  assert.match(sourcesRule, /box-sizing:\s*border-box/,
    'section padding must be included in its width, even outside the app reset')
  assert.match(listRule, /display:\s*grid/,
    'expanded references should use the established grid')
  assert.match(listRule, /grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\)/,
    'wide reference disclosures should use two equal columns')
  assert.match(listRule, /min-width:\s*0/,
    'the grid must be allowed to shrink long source titles')
  assert.match(listRule, /width:\s*100%/,
    'the two source columns should fill the available reading width')
  assert.match(listRule, /max-width:\s*100%/,
    'the source list must stay within its section')
  assert.match(listRule, /margin:\s*0/,
    'browser list margins must not push source cards out of alignment')
  assert.match(listRule, /padding:\s*0/,
    'browser list indentation must not reduce the source card width')
  assert.match(itemRule, /min-width:\s*0/,
    'a grid child must be allowed to shrink long source titles')
  assert.match(itemRule, /width:\s*100%/,
    'each grid item should fill its column')
  assert.match(nonWebItemRule, /grid-column:\s*1\s*\/\s*-1/,
    'non-web citations should retain a full row instead of joining the source grid')
  assert.match(chipRule, /width:\s*100%/,
    'each source card should fill its grid column')
  assert.match(css,
    /@media\s*\(max-width:\s*340px\)\s*\{\s*\.chat__sources-list\s*\{\s*grid-template-columns:\s*minmax\(0,\s*1fr\)/s,
    'narrow panes should fall back to one reference column')
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

test('queued row actions share full touch targets with compact visible wells', () => {
  const css = stripComments(chatCss)
  const trayRule = css.match(/\.queued\s*\{[^}]*\}/)?.[0] || ''
  const rowRule = css.match(/\.queued__row\s*\{[^}]*\}/)?.[0] || ''
  const toggleRule = css.match(/\.queued__toggle\s*\{[^}]*\}/)?.[0] || ''
  const actionRule = css.match(/\.queued__action\s*\{[^}]*\}/)?.[0] || ''
  const wellRule = css.match(/\.queued__action::before\s*\{[^}]*\}/)?.[0] || ''
  const iconRule = css.match(/\.queued__action svg\s*\{[^}]*\}/)?.[0] || ''
  const steerRule = css.match(/\.queued__steer\s*\{[^}]*\}/)?.[0] || ''
  const cancelRule = css.match(/\.queued__cancel\s*\{[^}]*\}/)?.[0] || ''
  const focusWellRule = css.match(/\.queued__action:focus-visible::before\s*\{[^}]*\}/)?.[0] || ''

  assert.match(trayRule, /width:\s*100%/,
    'the flex-item tray must shrink with the composer instead of resolving to its 720px maximum')
  assert.match(trayRule, /max-width:\s*720px/)
  assert.match(rowRule, /gap:\s*0/,
    'adjacent action targets should not carry an extra flex gap')
  assert.match(toggleRule, /margin-right:\s*4px/,
    'text keeps its breathing room independently of the adjacent action pair')
  assert.match(actionRule, /width:\s*44px/)
  assert.match(actionRule, /height:\s*44px/)
  assert.match(wellRule, /width:\s*30px/)
  assert.match(wellRule, /height:\s*30px/)
  assert.match(wellRule, /transform:\s*translateX\(var\(--queued-action-visual-shift\)\)/)
  assert.match(iconRule, /transform:\s*translateX\(var\(--queued-action-visual-shift\)\)/,
    'the well and icon should move together inside the stationary touch target')
  assert.match(steerRule, /--queued-action-visual-shift:\s*3px/)
  assert.match(cancelRule, /--queued-action-visual-shift:\s*-3px/,
    'the two 30px visuals move inward while their 44px targets remain adjacent')
  assert.doesNotMatch(steerRule, /\bcolor:|\bbackground:/,
    'fast-forward should inherit the neutral action treatment at rest')
  assert.doesNotMatch(css, /\.queued__steer::before\s*\{/,
    'fast-forward should inherit the neutral action well at rest')
  assert.match(css, /\.queued__steer:not\(:disabled\):hover/,
    'disabled fast-forward controls should not pick up hover emphasis')
  assert.match(focusWellRule, /box-shadow:\s*0 0 0 2px var\(--accent\)/,
    'keyboard focus should follow the visible well inside the full touch target')
  assert.match(queuedMessages, /className="queued__action queued__steer"/)
  assert.match(queuedMessages, /className="queued__action queued__cancel"/)
  assert.match(queuedMessages, /<DoubleChevronRight width=\{16\} height=\{16\}/)
  assert.match(queuedMessages, /<X width=\{16\} height=\{16\}/)
})

test('queued message content participates in native text selection', () => {
  const css = stripComments(chatCss)
  const toggleRule = css.match(/\.queued__toggle\s*\{[^}]*\}/)?.[0] || ''

  assert.match(toggleRule, /user-select:\s*text/,
    'desktop drag selection should include queued user text')
  assert.match(toggleRule, /-webkit-user-select:\s*text/,
    'WebKit should not inherit the global button selection lock')
  assert.match(toggleRule, /-webkit-touch-callout:\s*default/,
    'touch selection should keep the native action menu')
  assert.match(queuedMessages, /const MessageSurface = needsTruncation \? 'button' : 'div'/,
    'a non-disclosure queued message should be ordinary selectable content')
  assert.match(
    queuedMessages,
    /event\.detail !== 0[\s\S]*pointerSelectionChangedWithin\([\s\S]*event\.currentTarget[\s\S]*\) return[\s\S]*toggle\(key\)/,
    'selecting a long queued message must not also toggle its disclosure',
  )
  assert.doesNotMatch(
    queuedMessages,
    /className="queued__toggle"[\s\S]{0,160}onPointerDown=\{\(e\) => e\.preventDefault\(\)/,
    'the queued message surface must not cancel selection at pointer-down',
  )
})
