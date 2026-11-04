import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'

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

// The static boot document runs before React exists. Its inline scripts are
// executed in a vm against a minimal fake DOM so the timing rules for the
// boot notices are exercised, not matched. tests/bootstrap.spec.mjs covers
// the same rules against a real Chromium build.
const MAIN_ENTRY = readFileSync(new URL('../../main.jsx', import.meta.url), 'utf8')
const INDEX_BODY = INDEX_HTML.slice(INDEX_HTML.indexOf('<body'))
const INLINE_SCRIPTS = [...INDEX_BODY.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(match => match[1])
const BOOT_SCRIPT = INLINE_SCRIPTS.find(source => source.includes('function checkBoot'))
const ENTRY = 'https://mobius.test/assets/index.js'
const PRELOAD = 'https://mobius.test/assets/client.js'

function fakeElement(tagName, props = {}) {
  return {
    tagName,
    style: {},
    children: [],
    isConnected: true,
    setAttribute() {},
    append(...nodes) { this.children.push(...nodes) },
    addEventListener() {},
    remove() { this.isConnected = false },
    ...props,
  }
}

// One page load with a controllable clock, network, and React mount.
function bootPage() {
  let now = 0
  const timers = []
  const settled = new Map()
  const windowListeners = []
  const rootObservers = []
  const notices = []
  const root = { mounted: false, hasChildNodes() { return this.mounted } }
  const splash = fakeElement('DIV')
  const entryScript = fakeElement('SCRIPT', { type: 'module', src: ENTRY })
  const preloadLink = fakeElement('LINK', { href: PRELOAD })
  const document = {
    getElementById: id => (id === 'root' ? root : id === 'splash' && splash.isConnected ? splash : null),
    querySelectorAll: () => [entryScript, preloadLink],
    createElement: tag => fakeElement(tag.toUpperCase()),
    body: { appendChild: node => notices.push(node) },
  }
  const window = {
    location: { pathname: '/shell/', href: 'https://mobius.test/shell/', reload() {} },
    addEventListener: (type, fn, capture) => windowListeners.push({ type, fn, capture }),
  }
  class MutationObserver {
    constructor(callback) { this.callback = callback; this.active = false }
    observe(target, options) {
      assert.equal(target, root)
      assert.equal(options.childList, true)
      this.active = true
      rootObservers.push(this)
    }
    disconnect() { this.active = false }
  }
  const context = {
    window,
    document,
    history: {},
    MutationObserver,
    performance: {
      now: () => now,
      getEntriesByName: url => (settled.has(url) ? [{ responseEnd: settled.get(url) }] : []),
    },
    setTimeout: (fn, ms) => timers.push({ at: now + ms, fn }),
  }
  vm.runInNewContext(BOOT_SCRIPT, context)

  const advanceTo = (ms) => {
    for (;;) {
      timers.sort((a, b) => a.at - b.at)
      if (!timers.length || timers[0].at > ms) break
      const timer = timers.shift()
      now = timer.at
      timer.fn()
    }
    now = ms
  }
  const visibleNotice = () => {
    const shown = notices.filter(node => node.isConnected)
    assert.ok(shown.length <= 1, 'at most one boot notice at a time')
    return shown[0] ? shown[0].children.map(part => (typeof part === 'string' ? part : part.textContent)).join('') : null
  }
  return {
    advanceTo,
    finishDownload: (url, atMs) => settled.set(url, atMs),
    mount: () => {
      root.mounted = true
      for (const observer of rootObservers) if (observer.active) observer.callback([], observer)
    },
    failEntryFetch: () => windowListeners
      .filter(listener => listener.type === 'error' && listener.capture)
      .forEach(listener => listener.fn({ target: entryScript })),
    // main.jsx's pre-render session exchange, settled by the returned function.
    startSessionExchange: () => {
      let settle
      const exchange = new Promise(resolve => { settle = resolve })
      const observed = window.__mobiusBootSessionExchange(exchange)
      return async () => { settle(); await observed }
    },
    get notice() { return visibleNotice() },
    get failureShown() { return /failed to load/.test(visibleNotice() ?? '') },
    get coverPresent() { return splash.isConnected },
  }
}

test('a first visit still downloading its entry modules is not reported as a failed boot', () => {
  const page = bootPage()
  page.finishDownload(PRELOAD, 6_000)
  page.advanceTo(30_000)
  assert.equal(page.notice, null, 'bytes still arriving at 30s')
  assert.equal(page.coverPresent, true)

  page.finishDownload(ENTRY, 31_000)
  page.advanceTo(31_200)
  page.mount()
  page.advanceTo(59_000)
  assert.equal(page.notice, null)
})

test('entry modules that arrived but mounted nothing surface the reload notice', () => {
  const page = bootPage()
  page.finishDownload(ENTRY, 3_000)
  page.finishDownload(PRELOAD, 3_000)
  page.advanceTo(8_000)
  assert.equal(page.failureShown, true)
  assert.equal(page.coverPresent, false, 'the notice must not sit under the launch cover')
})

test('modules that just finished downloading get a mount grace before counting as broken', () => {
  const page = bootPage()
  page.finishDownload(PRELOAD, 2_000)
  page.finishDownload(ENTRY, 7_900)
  page.advanceTo(10_000)
  assert.equal(page.notice, null)
  page.advanceTo(12_000)
  assert.equal(page.failureShown, true)
})

test('a failed entry module fetch surfaces the reload notice without waiting', () => {
  const page = bootPage()
  page.advanceTo(1_000)
  page.failEntryFetch()
  assert.equal(page.failureShown, true)
})

test('a download still running at 60s is called slow, not failed, and keeps the cover', () => {
  const page = bootPage()
  page.advanceTo(58_000)
  assert.equal(page.notice, null)
  page.advanceTo(62_000)
  assert.match(page.notice, /still loading on a slow connection\. Try again or keep waiting\./)
  assert.equal(page.failureShown, false)
  assert.equal(page.coverPresent, true)
})

test('a slow notice leaves once the shell mounts after it appeared', () => {
  const page = bootPage()
  page.advanceTo(61_000)
  assert.ok(page.notice)
  page.finishDownload(ENTRY, 75_000)
  page.finishDownload(PRELOAD, 75_000)
  page.advanceTo(76_000)
  page.mount()
  assert.equal(page.notice, null)
  page.advanceTo(120_000)
  assert.equal(page.notice, null)
})

test('a slow download that arrives and still mounts nothing becomes a failure', () => {
  const page = bootPage()
  page.advanceTo(61_000)
  page.finishDownload(ENTRY, 70_000)
  page.finishDownload(PRELOAD, 70_000)
  page.advanceTo(80_000)
  assert.equal(page.failureShown, true)
  assert.equal(page.coverPresent, false)
})

test('a failure notice leaves if the shell mounts after all', () => {
  const page = bootPage()
  page.finishDownload(ENTRY, 3_000)
  page.finishDownload(PRELOAD, 3_000)
  page.advanceTo(8_000)
  assert.equal(page.failureShown, true)
  page.mount()
  assert.equal(page.notice, null)
})

test('the installed shell session exchange before first render counts as loading', async () => {
  assert.match(
    MAIN_ENTRY,
    /await window\.__mobiusBootSessionExchange\(redeemInstalledShellSession\(\)\)/,
    'main.jsx must report its pre-render network wait to the boot check',
  )
  const page = bootPage()
  page.finishDownload(ENTRY, 3_000)
  page.finishDownload(PRELOAD, 3_000)
  page.advanceTo(3_100)
  const settleExchange = page.startSessionExchange()
  page.advanceTo(30_000)
  assert.equal(page.notice, null, 'a slow exchange is network time, not a stalled mount')

  await settleExchange()
  page.advanceTo(32_000)
  assert.equal(page.notice, null, 'the mount grace restarts when the exchange settles')
  page.advanceTo(36_000)
  assert.equal(page.failureShown, true, 'nothing mounted after the exchange is still a failure')
})
