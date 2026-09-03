/* Browser pinch stays locked; intentional desktop author zoom owns shell density. */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const indexHtml = readFileSync(new URL('../../../index.html', import.meta.url), 'utf8')
const indexCss = readFileSync(new URL('../../index.css', import.meta.url), 'utf8')
const shellCss = readFileSync(new URL('../../components/Shell/Shell.css', import.meta.url), 'utf8')
const appFrameHtml = readFileSync(new URL('../../../public/app-frame.html', import.meta.url), 'utf8')
const offlineHtml = readFileSync(new URL('../../../public/offline.html', import.meta.url), 'utf8')
const appCanvas = readFileSync(
  new URL('../../components/AppCanvas/AppCanvas.jsx', import.meta.url),
  'utf8',
)
const applyTheme = readFileSync(new URL('../applyTheme.js', import.meta.url), 'utf8')
const standaloneRoute = readFileSync(
  new URL('../../../../backend/app/routes/standalone.py', import.meta.url),
  'utf8',
)
const viewportMetrics = readFileSync(
  new URL('../../../public/gesture-repros/metrics.js', import.meta.url),
  'utf8',
)
const buildingApps = readFileSync(
  new URL('../../../../backend/scripts/seed-skills/building-apps.md', import.meta.url),
  'utf8',
)

function viewportContent(html) {
  return html.match(/<meta name="viewport" content="([^"]+)"/)?.[1] || ''
}

test('desktop density belongs to the shell and never scales standalone apps', () => {
  assert.match(shellCss, /@media \(min-width: 1024px\)[\s\S]*:root\s*\{[\s\S]*zoom:\s*0\.9/)
  assert.doesNotMatch(indexCss, /:root\s*\{[^}]*zoom:/)
})

test('browser pinch cannot scale the shell chrome and active app together', () => {
  const viewport = viewportContent(indexHtml)
  assert.match(viewport, /width=device-width/)
  assert.match(viewport, /initial-scale=1(?:\.0)?/)
  assert.match(viewport, /maximum-scale=1/)
  assert.match(viewport, /user-scalable=no/)
  assert.match(viewport, /viewport-fit=cover/)
  assert.match(viewport, /interactive-widget=resizes-content/)

  const rootTouchRule = indexCss.match(
    /html,\s*body\s*\{\s*touch-action:[^}]+\}/,
  )?.[0] || ''
  assert.match(rootTouchRule, /touch-action:\s*pan-x pan-y/)
  assert.doesNotMatch(rootTouchRule, /pinch-zoom|manipulation/)
})

test('the app frame adds no second viewport lock and leaves local zoom to the app', () => {
  const viewport = viewportContent(appFrameHtml)
  assert.match(viewport, /width=device-width/)
  assert.match(viewport, /initial-scale=1(?:\.0)?/)
  // The shared app frame must NOT globally forbid zoom — that choice belongs to
  // each app's own surface (touch-action:none + transform on its own content).
  assert.doesNotMatch(viewport, /user-scalable=no/)
  assert.doesNotMatch(viewport, /maximum-scale=1/)

  const frameRootTouchRule = appFrameHtml.match(/html,\s*body\s*\{\s*touch-action:[^}]+\}/)?.[0] || ''
  assert.doesNotMatch(frameRootTouchRule, /touch-action:\s*pan-x pan-y/)
})

test('app authors are told to zoom content locally, with an accessible control path', () => {
  assert.match(buildingApps, /## Local zoom surfaces/)
  assert.match(buildingApps, /touch-action: none/)
  assert.match(buildingApps, /transform only the\s+content/)
  assert.match(buildingApps, /zoom-in, zoom-out, and reset controls/)
  assert.match(buildingApps, /Do not add `user-scalable=no`/)
})

test('installed shell owns one stable edge-to-edge iOS viewport', () => {
  assert.match(
    indexHtml,
    /apple-mobile-web-app-status-bar-style" content="black-translucent"/,
  )
  assert.match(
    indexCss,
    /@media \(display-mode: standalone\)[\s\S]*html,[\s\S]*body\s*\{\s*height:\s*100vh/,
  )
  assert.match(indexCss, /#root\s*\{\s*height:\s*100%/)
  assert.doesNotMatch(
    applyTheme,
    /querySelector\(['"]meta\[name=["']apple-mobile-web-app-status-bar-style/,
  )
  assert.match(
    offlineHtml,
    /apple-mobile-web-app-status-bar-style" content="black-translucent"/,
  )
  assert.match(
    offlineHtml,
    /@media \(display-mode:standalone\)[\s\S]*height:100vh/,
  )
})

test('immersive insets follow resume geometry and fullscreen stays a request', () => {
  for (const signal of [
    "window.addEventListener('pageshow'",
    "document.addEventListener('visibilitychange'",
    "viewport?.addEventListener('resize'",
    "viewport?.addEventListener('scroll'",
  ]) {
    assert.ok(appCanvas.includes(signal), `missing geometry signal: ${signal}`)
  }
  assert.match(buildingApps, /iOS accepts the display mode but can retain its OS status bar/)
  assert.match(buildingApps, /opaque app frame, where direct `env\(\)` values may be zero/)
  assert.match(standaloneRoute, /iOS may still\s+retain its OS status bar/)
  assert.doesNotMatch(
    standaloneRoute,
    /"fullscreen" additionally drops the OS status bar/,
  )
})

test('viewport lab records resume, VisualViewport, root rects, and insets', () => {
  for (const signal of [
    'visualViewport.resize',
    'visualViewport.scroll',
    'pageshow',
    'visibilitychange',
    'html rect',
    'root rect',
    'safe insets T/R/B/L',
  ]) {
    assert.ok(viewportMetrics.includes(signal), `missing viewport metric: ${signal}`)
  }
})
