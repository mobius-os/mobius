/* Browser pinch stays locked; intentional desktop author zoom owns shell density. */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const indexHtml = readFileSync(new URL('../../../index.html', import.meta.url), 'utf8')
const indexCss = readFileSync(new URL('../../index.css', import.meta.url), 'utf8')
const shellCss = readFileSync(
  new URL('../../components/Shell/Shell.css', import.meta.url),
  'utf8',
)
const appFrameHtml = readFileSync(new URL('../../../public/app-frame.html', import.meta.url), 'utf8')
const buildingApps = readFileSync(
  new URL('../../../../backend/scripts/seed-skills/building-apps.md', import.meta.url),
  'utf8',
)

function viewportContent(html) {
  return html.match(/<meta name="viewport" content="([^"]+)"/)?.[1] || ''
}

test('desktop web keeps its intentional 90% author zoom in one policy', () => {
  const desktop = shellCss.match(/@media \(min-width: 1024px\) \{[\s\S]*$/)?.[0] || ''
  assert.match(desktop, /--desktop-shell-density:\s*0\.9/)
  assert.match(desktop, /zoom:\s*var\(--desktop-shell-density\)/)
})

test('browser pinch cannot scale the shell chrome and active app together', () => {
  const viewport = viewportContent(indexHtml)
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
