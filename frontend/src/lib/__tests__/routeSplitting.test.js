import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const SOURCE = readFileSync(new URL('../../App.jsx', import.meta.url), 'utf8')
const API_CLIENT = readFileSync(new URL('../../api/client.js', import.meta.url), 'utf8')
const LOGIN_CSS = readFileSync(
  new URL('../../components/LoginForm/LoginForm.css', import.meta.url),
  'utf8',
)
const SHELL = readFileSync(new URL('../../components/Shell/Shell.jsx', import.meta.url), 'utf8')
const CHAT_VIEW = readFileSync(
  new URL('../../components/ChatView/ChatView.jsx', import.meta.url),
  'utf8',
)
const INDEX_HTML = readFileSync(new URL('../../../index.html', import.meta.url), 'utf8')
const STATIC_INDEX_STYLES = [...INDEX_HTML.matchAll(/<style(?:\s[^>]*)?>([\s\S]*?)<\/style>/g)]
  .map(match => match[1])
  .join('\n')
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

// tests/app-canvas.spec.mjs finds these chunks by the name Vite gives a lazily
// imported module (AppCanvas-<hash>.js), so keep each a module of its own.
test('surfaces a chat-only launch never paints stay out of the shell startup code', () => {
  const deferred = [
    [SHELL, [
      'AppCanvas', 'ProjectsDirectory', 'ProjectWorkspace', 'AppSourceWorkspace',
      'ArtifactWorkspace', 'ProjectCopyImport',
    ]],
    [CHAT_VIEW, [
      'AgentContextInspector', 'ContextPanel', 'ChatSummaryViewer', 'ChatDiffViewer',
      'ChatUsageInspector', 'ChatNetworkInspector',
    ]],
  ]
  for (const [source, components] of deferred) {
    for (const component of components) {
      assert.match(
        source,
        new RegExp(`const ${component} = lazy\\(\\(\\) => import\\('[^']*/${component}\\.jsx'\\)\\)`),
        `${component} must load on demand`,
      )
      assert.doesNotMatch(
        source,
        new RegExp(`^import ${component}\\b`, 'm'),
        `${component} must not return to the shared startup bundle`,
      )
    }
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

test('cross-document continuity is limited to controlled shell refreshes', () => {
  assert.match(INDEX_HTML, /navigator\.serviceWorker\.register\('\/sw\.js'/)
  assert.doesNotMatch(INDEX_HTML, /registration\.update|serviceWorker\.ready/)
  assert.doesNotMatch(
    INDEX_HTML,
    /navigator\.serviceWorker\.addEventListener\(\s*['"]controllerchange['"]/,
  )
  // A document-wide static opt-in would capture ordinary links, auth
  // redirects, and Back/Forward. The controlled refresh may inject this rule
  // only immediately before its own replacement navigation.
  assert.doesNotMatch(STATIC_INDEX_STYLES, /@view-transition\s*\{\s*navigation:/)
  assert.match(INDEX_HTML, /style\.textContent = '@view-transition \{ navigation: auto; \}'/)
  assert.match(INDEX_HTML, /window\.__mobiusPrepareShellReloadTransition/)
  assert.match(INDEX_HTML, /window\.addEventListener\('pageswap'/)
  assert.match(INDEX_HTML, /window\.__mobiusReleaseShellReloadTransition/)
  assert.match(SOURCE, /__mobiusReleaseShellReloadTransition/)
  assert.match(SOURCE, /splash\.style\.opacity = '0'/)
})

test('mobius.you sign-in stays on the viewed origin and owns a centered action', () => {
  assert.match(
    API_CLIENT,
    /startUrl:\s*\(\)\s*=>\s*['"]\/api\/auth\/mobius\/login\/start['"]/,
  )
  assert.doesNotMatch(
    API_CLIENT,
    /startUrl:\s*\(\)\s*=>\s*`\$\{BASE\}\/api\/auth\/mobius\/login\/start`/,
  )
  const buttonRule = LOGIN_CSS.match(/\.login__btn\s*\{[\s\S]*?\}/)?.[0] || ''
  assert.match(buttonRule, /width:\s*100%/)
  assert.match(buttonRule, /justify-content:\s*center/)
})

test('the launch cover stays up while any visible surface is still downloading', async () => {
  const { createLaunchCoverHold } = await import('../../components/ErrorBoundary/LazySurface.jsx')
  const frames = []
  let published = 0
  const cover = createLaunchCoverHold(() => { published += 1 }, callback => frames.push(callback))

  const release = cover.hold()
  cover.request()
  assert.equal(published, 0, 'a pending surface holds the cover')

  // An effect re-run releases and re-acquires before the next frame.
  release()
  const reacquired = cover.hold()
  frames.splice(0).forEach(frame => frame())
  assert.equal(published, 0, 'the queued frame must not lift the cover over a pending surface')

  reacquired()
  frames.splice(0).forEach(frame => frame())
  assert.equal(published, 1, 'the cover lifts a frame after the last surface is ready')
})

test('the launch cover lifts at once when no surface is pending', async () => {
  const { createLaunchCoverHold } = await import('../../components/ErrorBoundary/LazySurface.jsx')
  let published = 0
  const cover = createLaunchCoverHold(() => { published += 1 }, () => {
    throw new Error('no frame is needed without a pending surface')
  })
  cover.request()
  assert.equal(published, 1)
})
