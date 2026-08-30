import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const SOURCE = readFileSync(new URL('../../App.jsx', import.meta.url), 'utf8')
const API_CLIENT = readFileSync(new URL('../../api/client.js', import.meta.url), 'utf8')
const LOGIN_CSS = readFileSync(
  new URL('../../components/LoginForm/LoginForm.css', import.meta.url),
  'utf8',
)
const INDEX_HTML = readFileSync(new URL('../../../index.html', import.meta.url), 'utf8')
const RECOVERY_CSS = [
  readFileSync(new URL('../../components/ErrorBoundary/ErrorBoundary.css', import.meta.url), 'utf8'),
  readFileSync(new URL('../../components/ErrorBoundary/RecoveryPanel.css', import.meta.url), 'utf8'),
].join('\n')

test('mutually exclusive top-level flows remain lazy route boundaries', () => {
  for (const component of ['SetupWizard', 'LoginForm', 'Shell', 'ChatEmbed']) {
    assert.match(
      SOURCE,
      new RegExp(`const ${component} = lazy\\(\\(\\) => import\\(`),
      `${component} must not return to the shared startup bundle`,
    )
  }
  const startupError = SOURCE.slice(
    SOURCE.indexOf('function StartupError'),
    SOURCE.indexOf('function removeSplash'),
  )
  const sharedClasses = [
    'errbound__card',
    'recovery-panel--boundary',
    'recovery-panel__title',
    'recovery-panel__body',
    'recovery-panel__actions',
    'recovery-panel__button',
    'recovery-panel__button--primary',
  ]

  assert.ok((SOURCE.match(/<StartupError/g) || []).length >= 2)
  assert.doesNotMatch(startupError, /errbound__(?:title|body|actions|btn)/)
  for (const className of sharedClasses) {
    assert.match(startupError, new RegExp(`className="[^"]*${className}`))
    assert.match(
      RECOVERY_CSS,
      new RegExp(`\\.${className.replaceAll('-', '\\-')}(?:[\\s.:,{]|$)`),
      `${className} must keep a stylesheet rule`,
    )
  }
})

test('route loading and the theme-matched launch cover contain no artwork', () => {
  const routeLoading = SOURCE.slice(
    SOURCE.indexOf('function RouteLoading'),
    SOURCE.indexOf('function StartupError'),
  )
  const launchSplash = INDEX_HTML.slice(
    INDEX_HTML.indexOf('<div id="splash"'),
    INDEX_HTML.indexOf('<div id="root"'),
  )

  assert.doesNotMatch(routeLoading, /Möbius|moebius\.(?:png|svg)|<img|<span|role="status"/)
  assert.match(routeLoading, /return <div className="app-route-loading" aria-hidden="true" \/>/)
  assert.match(launchSplash, /<div id="splash" aria-hidden="true"/)
  assert.doesNotMatch(
    launchSplash,
    /Möbius|moebius\.(?:png|svg)|<img|<svg|<span/,
    'the startup handoff must remain an artwork-free theme cover',
  )
})

test('managed sign-in stays on the viewed origin and owns a centered action', () => {
  assert.match(
    API_CLIENT,
    /startUrl:\s*\(returnPath = '\/'\)\s*=>\s*\(\s*`\/api\/auth\/sso\/start\?return_path=/,
  )
  assert.doesNotMatch(
    API_CLIENT,
    /startUrl:\s*\(returnPath = '\/'\)\s*=>\s*\(\s*`\$\{BASE\}\/api\/auth\/sso\/start/,
  )
  const buttonRule = LOGIN_CSS.match(/\.login__btn\s*\{[\s\S]*?\}/)?.[0] || ''
  assert.match(buttonRule, /width:\s*100%/)
  assert.match(buttonRule, /justify-content:\s*center/)
})
