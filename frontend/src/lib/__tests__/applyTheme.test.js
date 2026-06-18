/**
 * Unit tests for the shared theme library, src/lib/applyTheme.js.
 *
 * Run with:
 *   cd frontend && node --test src/lib/__tests__/applyTheme.test.js
 *
 * applyTheme.js is the single source of truth for painting a theme onto
 * the DOM (shared by themeService.applyThemeToDom and, in spirit, the
 * app-frame). We drive it against the same tiny DOM stub themeService.test.js
 * uses instead of pulling in jsdom.
 */
import { test, beforeEach } from 'node:test'
import assert from 'node:assert/strict'

import { inferMode, resolveTheme, applyTheme, HEX_RE, PREPAINT_SRC } from '../applyTheme.js'

// --- inferMode ---------------------------------------------------------

test('inferMode classifies dark and light by luminance', () => {
  assert.equal(inferMode('#0d0d0d'), 'dark')
  assert.equal(inferMode('#f0eeeb'), 'light')
  assert.equal(inferMode('#000000'), 'dark')
  assert.equal(inferMode('#ffffff'), 'light')
})

test('inferMode expands 3- and 4-digit hex', () => {
  assert.equal(inferMode('#fff'), 'light')   // -> #ffffff
  assert.equal(inferMode('#000'), 'dark')    // -> #000000
  assert.equal(inferMode('#ffff'), 'light')  // RGBA -> RGB #fff
  assert.equal(inferMode('#000f'), 'dark')   // RGBA -> RGB #000
})

test('inferMode drops the alpha byte of 8-digit hex', () => {
  assert.equal(inferMode('#ffffff00'), 'light')
  assert.equal(inferMode('#00000000'), 'dark')
})

test('inferMode returns null for missing / non-hex bg', () => {
  assert.equal(inferMode(undefined), null)
  assert.equal(inferMode(null), null)
  assert.equal(inferMode(''), null)
  assert.equal(inferMode('expression(alert(1))'), null)
  assert.equal(inferMode('rgb(0,0,0)'), null)
})

// --- DOM stub (same shape as themeService.test.js) ---------------------

function makeDomStub() {
  const styleNodes = new Map()
  const fontLinks = []
  const headChildren = []
  const meta = { content: '#000000', getAttribute: (k) => meta[k], setAttribute: (k, v) => { meta[k] = v } }
  const statusBar = { content: 'black', getAttribute: (k) => statusBar[k], setAttribute: (k, v) => { statusBar[k] = v } }
  const body = { style: {} }
  const documentElement = {
    _attrs: {},
    getAttribute: (k) => documentElement._attrs[k],
    setAttribute: (k, v) => { documentElement._attrs[k] = v },
    style: {
      _props: {},
      colorScheme: '',
      setProperty(name, value) { this._props[name] = value },
      getPropertyValue(name) { return this._props[name] },
    },
  }
  function makeNode(tag) {
    return {
      tagName: tag.toUpperCase(),
      id: '', rel: '', href: '', textContent: '', dataset: {},
      remove() {
        if (this.tagName === 'STYLE') styleNodes.delete(this.id)
        if (this.tagName === 'LINK') {
          const i = fontLinks.indexOf(this); if (i >= 0) fontLinks.splice(i, 1)
          const j = headChildren.indexOf(this); if (j >= 0) headChildren.splice(j, 1)
        }
      },
    }
  }
  const slotNodes = new Map()  // id -> { textContent }
  return {
    document: {
      createElement(tag) { return makeNode(tag) },
      getElementById(id) { return styleNodes.get(id) || slotNodes.get(id) || null },
      querySelector(sel) {
        if (sel === 'meta[name="theme-color"]') return meta
        if (sel.includes('apple-mobile-web-app-status-bar-style')) return statusBar
        return null
      },
      querySelectorAll(sel) {
        if (sel.includes('data-theme-font')) return fontLinks.slice()
        return []
      },
      head: {
        appendChild(node) {
          if (node.tagName === 'STYLE' && node.id) styleNodes.set(node.id, node)
          if (node.tagName === 'LINK' && node.dataset.themeFont) fontLinks.push(node)
          const i = headChildren.indexOf(node)
          if (i >= 0) headChildren.splice(i, 1)
          headChildren.push(node)
          return node
        },
        get children() { return headChildren },
      },
      body,
      documentElement,
    },
    meta, statusBar, documentElement, fontLinks, headChildren, styleNodes, slotNodes,
  }
}

function makeStoreStub() {
  const map = new Map()
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => { map.set(k, String(v)) },
    removeItem: (k) => { map.delete(k) },
    _map: map,
  }
}

let dom, store
beforeEach(() => { dom = makeDomStub(); store = makeStoreStub() })

const ctx = () => ({ doc: dom.document, store })

// --- applyTheme: DOM mutations ----------------------------------------

test('applyTheme injects CSS into <style id="mobius-theme">', () => {
  applyTheme({ css: ':root { --bg: #123456; }', bg: '#123456' }, ctx())
  const el = dom.styleNodes.get('mobius-theme')
  assert.ok(el)
  assert.equal(el.textContent, ':root { --bg: #123456; }')
})

test('applyTheme re-appends the style node so it wins the cascade', () => {
  applyTheme({ css: ':root { --bg: #111; }', bg: '#111111' }, ctx())
  const first = dom.styleNodes.get('mobius-theme')
  const intruder = dom.document.createElement('style'); intruder.id = 'x'
  dom.document.head.appendChild(intruder)
  applyTheme({ css: ':root { --bg: #222; }', bg: '#222222' }, ctx())
  const order = dom.headChildren
  assert.equal(order[order.length - 1], first)
})

test('applyTheme strips @import and converts safe ones to <link>', () => {
  applyTheme({ css: "@import url('https://fonts.example/x.css');\n:root{--bg:#abc;}", bg: '#aabbcc' }, ctx())
  assert.equal(dom.fontLinks.length, 1)
  assert.equal(dom.fontLinks[0].href, 'https://fonts.example/x.css')
  assert.equal(dom.fontLinks[0].rel, 'stylesheet')
  assert.equal(dom.fontLinks[0].dataset.themeFont, '1')
  assert.ok(!dom.styleNodes.get('mobius-theme').textContent.includes('@import'))
})

test('applyTheme rejects javascript:/data: @import URLs (allowlist)', () => {
  applyTheme({ css: "@import url('javascript:alert(1)');@import url('data:text/css,x');:root{}", bg: '#000000' }, ctx())
  assert.equal(dom.fontLinks.length, 0)
})

test('applyTheme removes prior font links before adding new ones', () => {
  applyTheme({ css: "@import url('https://a.example/x.css');:root{}", bg: '#000000' }, ctx())
  applyTheme({ css: "@import url('https://b.example/y.css');:root{}", bg: '#000000' }, ctx())
  assert.equal(dom.fontLinks.length, 1)
  assert.equal(dom.fontLinks[0].href, 'https://b.example/y.css')
})

test('applyTheme sets bg on body, meta theme-color, inline --bg', () => {
  applyTheme({ css: ':root{}', bg: '#abcdef' }, ctx())
  assert.equal(dom.document.body.style.background, '#abcdef')
  assert.equal(dom.meta.content, '#abcdef')
  assert.equal(dom.documentElement.style.getPropertyValue('--bg'), '#abcdef')
})

test('applyTheme ignores a non-hex bg (no DOM / no persist mutation)', () => {
  dom.document.body.style.background = '#init'
  dom.meta.content = '#init'
  applyTheme({ css: ':root{}', bg: 'expression(alert(1))' }, ctx())
  assert.equal(dom.document.body.style.background, '#init')
  assert.equal(dom.meta.content, '#init')
  assert.equal(store.getItem('mobius-theme'), null)
  assert.equal(store.getItem('mobius-theme-bg'), null)
})

test('applyTheme sets data-theme + color-scheme + status bar from mode', () => {
  applyTheme({ css: ':root{--bg:#f0eeeb;}', bg: '#f0eeeb' }, ctx())  // light
  assert.equal(dom.documentElement.getAttribute('data-theme'), 'light')
  assert.equal(dom.documentElement.style.colorScheme, 'light')
  assert.equal(dom.statusBar.content, 'default')

  applyTheme({ css: ':root{--bg:#0d0d0d;}', bg: '#0d0d0d' }, ctx())  // dark
  assert.equal(dom.documentElement.getAttribute('data-theme'), 'dark')
  assert.equal(dom.documentElement.style.colorScheme, 'dark')
  assert.equal(dom.statusBar.content, 'black')
})

test('applyTheme honours an explicit mode over inferred', () => {
  // A light bg but mode explicitly dark — the explicit mode wins.
  applyTheme({ css: ':root{}', bg: '#f0eeeb', mode: 'dark' }, ctx())
  assert.equal(dom.documentElement.getAttribute('data-theme'), 'dark')
})

test('applyTheme infers mode from --bg in CSS when no bg arg', () => {
  applyTheme({ css: ':root { --bg: #f0eeeb; }' }, ctx())
  assert.equal(dom.documentElement.getAttribute('data-theme'), 'light')
})

test('applyTheme persists both mobius-theme and mobius-theme-bg', () => {
  applyTheme({ css: ':root{--bg:#f0eeeb;}', bg: '#f0eeeb' }, ctx())
  assert.deepEqual(JSON.parse(store.getItem('mobius-theme')), { bg: '#f0eeeb', mode: 'light' })
  assert.equal(store.getItem('mobius-theme-bg'), '#f0eeeb')
})

// --- resolveTheme: precedence -----------------------------------------

test('resolveTheme: 1) JSON slot wins', () => {
  dom.slotNodes.set('__mobius-theme__', { textContent: JSON.stringify({ css: ':root{--bg:#abc;}', bg: '#aabbcc', mode: 'light' }) })
  store.setItem('mobius-theme', JSON.stringify({ bg: '#0d0d0d', mode: 'dark' }))
  const t = resolveTheme(ctx())
  assert.equal(t.bg, '#aabbcc')
  assert.equal(t.mode, 'light')
  assert.equal(t.css, ':root{--bg:#abc;}')
})

test('resolveTheme: 2) mobius-theme when no slot', () => {
  store.setItem('mobius-theme', JSON.stringify({ bg: '#f0eeeb', mode: 'light' }))
  store.setItem('mobius-theme-bg', '#0d0d0d')
  const t = resolveTheme(ctx())
  assert.equal(t.bg, '#f0eeeb')
  assert.equal(t.mode, 'light')
  assert.equal(t.css, undefined)
})

test('resolveTheme: 3) legacy mobius-theme-bg when no slot/new key', () => {
  store.setItem('mobius-theme-bg', '#f0eeeb')
  const t = resolveTheme(ctx())
  assert.equal(t.bg, '#f0eeeb')
  assert.equal(t.mode, 'light')  // inferred from the bg
})

test('resolveTheme: 4) dark default when nothing present', () => {
  const t = resolveTheme(ctx())
  assert.deepEqual(t, { bg: '#0d0d0d', mode: 'dark' })
})

test('resolveTheme infers mode when the stored mode is missing', () => {
  store.setItem('mobius-theme', JSON.stringify({ bg: '#f0eeeb' }))
  assert.equal(resolveTheme(ctx()).mode, 'light')
})

test('resolveTheme tolerates malformed slot JSON (falls through)', () => {
  dom.slotNodes.set('__mobius-theme__', { textContent: '{not json' })
  store.setItem('mobius-theme', JSON.stringify({ bg: '#f0eeeb', mode: 'light' }))
  const t = resolveTheme(ctx())
  assert.equal(t.bg, '#f0eeeb')
})

test('resolveTheme guards a non-hex bg in the slot', () => {
  dom.slotNodes.set('__mobius-theme__', { textContent: JSON.stringify({ bg: 'evil', mode: 'light' }) })
  const t = resolveTheme(ctx())
  assert.equal(t.bg, '#0d0d0d')  // falls back to default bg, keeps slot mode
  assert.equal(t.mode, 'light')
})

// --- PREPAINT_SRC string ----------------------------------------------

test('PREPAINT_SRC is a self-contained IIFE with no imports', () => {
  assert.match(PREPAINT_SRC, /^\(function \(\) \{/)
  assert.match(PREPAINT_SRC, /\}\)\(\);$/)
  assert.ok(!PREPAINT_SRC.includes('import '))
  assert.ok(PREPAINT_SRC.includes('__mobius-theme__'))
  assert.ok(PREPAINT_SRC.includes('colorScheme'))
})

test('HEX_RE matches valid hex and rejects garbage', () => {
  assert.ok(HEX_RE.test('#0d0d0d'))
  assert.ok(HEX_RE.test('#fff'))
  assert.ok(!HEX_RE.test('rgb(0,0,0)'))
  assert.ok(!HEX_RE.test('#gg'))
})
