import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'vite'

const vite = await createServer({
  appType: 'custom',
  logLevel: 'error',
  server: { middlewareMode: true, hmr: false, ws: false },
  ssr: { noExternal: ['@openai/apps-sdk-ui'] },
})
const { scrollStripWheel } = await vite.ssrLoadModule('/src/components/Shell/PaneStrip.jsx')

after(() => vite.close())

function strip({ scrollWidth = 900, clientWidth = 300, zoom = 1 } = {}) {
  return {
    scrollWidth,
    clientWidth,
    scrollLeft: 0,
    currentCSSZoom: zoom,
    clientLeft: 0,
    clientTop: 0,
    getBoundingClientRect: () => ({ left: 0, top: 0, width: clientWidth, height: 32 }),
  }
}

function wheel(currentTarget, { deltaX = 0, deltaY = 0, deltaMode = 0 } = {}) {
  return { currentTarget, deltaX, deltaY, deltaMode }
}

test('a dominant vertical wheel pans an overflowing strip sideways', () => {
  const el = strip()
  scrollStripWheel(wheel(el, { deltaY: 40 }))
  assert.equal(el.scrollLeft, 40)
  scrollStripWheel(wheel(el, { deltaY: -15, deltaX: 5 }))
  assert.equal(el.scrollLeft, 25)
})

test('line and page wheel modes scale into pixels', () => {
  const lines = strip()
  scrollStripWheel(wheel(lines, { deltaY: 3, deltaMode: 1 }))
  assert.equal(lines.scrollLeft, 48)
  const pages = strip({ clientWidth: 250 })
  scrollStripWheel(wheel(pages, { deltaY: 1, deltaMode: 2 }))
  assert.equal(pages.scrollLeft, 250)
})

test('trackpad horizontal wheels and strips that fit stay native', () => {
  const el = strip()
  scrollStripWheel(wheel(el, { deltaX: 30, deltaY: 10 }))
  assert.equal(el.scrollLeft, 0, 'horizontal-dominant delta is the browser’s own pan')
  scrollStripWheel(wheel(el, { deltaX: 0, deltaY: 0 }))
  assert.equal(el.scrollLeft, 0)
  const fits = strip({ scrollWidth: 300, clientWidth: 300 })
  scrollStripWheel(wheel(fits, { deltaY: 40 }))
  assert.equal(fits.scrollLeft, 0, 'nothing hidden means nothing to reach')
})

test('pixel wheel deltas are read in layout space under document zoom', () => {
  const el = strip({ zoom: 2 })
  scrollStripWheel(wheel(el, { deltaY: 40 }))
  assert.equal(el.scrollLeft, 20)
})
