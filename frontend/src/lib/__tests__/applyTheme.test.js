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

import { inferMode, resolveTheme, applyTheme, HEX_RE, PREPAINT_SRC, colorSchemeMetaContent } from '../applyTheme.js'

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
  const colorScheme = { content: 'dark light', name: 'color-scheme', getAttribute: (k) => colorScheme[k], setAttribute: (k, v) => { colorScheme[k] = v } }
  const statusBar = { content: 'black', getAttribute: (k) => statusBar[k], setAttribute: (k, v) => { statusBar[k] = v } }
  const body = { style: {} }
  const documentElement = {
    _attrs: {},
    getAttribute: (k) => documentElement._attrs[k],
    setAttribute: (k, v) => { documentElement._attrs[k] = v },
    style: {
      _props: {},
      colorScheme: '',
      setProperty(name, value) { this._props[name] = value }, removeProperty(name) { delete this._props[name] },
      getPropertyValue(name) { return this._props[name] },
    },
  }
  function makeNode(tag) {
    return {
      tagName: tag.toUpperCase(),
      id: '', rel: '', href: '', textContent: '', dataset: {},
      setAttribute(k, v) { if (k === 'data-theme-font') this.dataset.themeFont = v; else this[k] = v },
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
        if (sel === 'meta[name="color-scheme"]') return colorScheme
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
    meta, colorScheme, statusBar, documentElement, fontLinks, headChildren, styleNodes, slotNodes,
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

test('applyTheme honors an explicit mode over inferring from bg (stale-SWR divergence fix)', () => {
  // A DARK bg paired with an explicit LIGHT mode — the shape a stale
  // /api/theme revalidation can feed the apply effect. The explicit mode must
  // win; otherwise data-theme/color-scheme stick dark while the toggle knob
  // (its own source) says light — the owner-reported "dark toggle on, light
  // UI" divergence. Now that /api/theme carries mode, the paint follows it.
  applyTheme({ css: ':root{--bg:#0d0d0d;}', bg: '#0d0d0d', mode: 'light' }, ctx())
  assert.equal(dom.document.documentElement.getAttribute('data-theme'), 'light')
  assert.equal(dom.document.documentElement.style.colorScheme, 'light')
  // Converse: explicit dark over a light bg.
  applyTheme({ css: ':root{--bg:#f0eeeb;}', bg: '#f0eeeb', mode: 'dark' }, ctx())
  assert.equal(dom.document.documentElement.getAttribute('data-theme'), 'dark')
  assert.equal(dom.document.documentElement.style.colorScheme, 'dark')
  // No explicit mode → still infers from bg (backward-compatible).
  applyTheme({ css: ':root{--bg:#0d0d0d;}', bg: '#0d0d0d' }, ctx())
  assert.equal(dom.document.documentElement.getAttribute('data-theme'), 'dark')
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

// All four CSS @import spellings must be stripped from the body and only the
// http(s) ones re-injected as <link> — the broadened allowlist closes the
// bare-url() / no-url() bypass the quoted-url-only regex had.
test('applyTheme allowlists a bare url() @import (one <link>)', () => {
  applyTheme({ css: '@import url(https://a.example/x.css);:root{}', bg: '#000000' }, ctx())
  assert.equal(dom.fontLinks.length, 1)
  assert.equal(dom.fontLinks[0].href, 'https://a.example/x.css')
  assert.ok(!dom.styleNodes.get('mobius-theme').textContent.includes('@import'))
})

test('applyTheme allowlists a no-url() quoted @import (one <link>)', () => {
  applyTheme({ css: '@import "https://b.example/y.css";:root{}', bg: '#000000' }, ctx())
  assert.equal(dom.fontLinks.length, 1)
  assert.equal(dom.fontLinks[0].href, 'https://b.example/y.css')
  assert.ok(!dom.styleNodes.get('mobius-theme').textContent.includes('@import'))
})

test('applyTheme drops a bare url(data:) @import (no <link>, stripped from body)', () => {
  applyTheme({ css: '@import url(data:text/css,x);:root{}', bg: '#000000' }, ctx())
  assert.equal(dom.fontLinks.length, 0)
  assert.ok(!dom.styleNodes.get('mobius-theme').textContent.includes('@import'))
})

test('applyTheme drops a no-url() "javascript:" @import (no <link>, stripped)', () => {
  applyTheme({ css: '@import "javascript:alert(1)";:root{}', bg: '#000000' }, ctx())
  assert.equal(dom.fontLinks.length, 0)
  assert.ok(!dom.styleNodes.get('mobius-theme').textContent.includes('@import'))
})

test('applyTheme handles all four @import spellings in one CSS body', () => {
  // quoted url(), bare url(), no-url() quoted, and a bare url(data:) that the
  // allowlist must drop — all stripped from the body, only the http(s) ones
  // become <link>.
  applyTheme({ css: "@import url('https://q.example/a.css');@import url(https://b.example/b.css);@import \"https://c.example/c.css\";@import url(data:text/css,x);:root{}", bg: '#000000' }, ctx())
  const hrefs = dom.fontLinks.map(l => l.href).sort()
  assert.deepEqual(hrefs, ['https://b.example/b.css', 'https://c.example/c.css', 'https://q.example/a.css'])
  assert.ok(!dom.styleNodes.get('mobius-theme').textContent.includes('@import'))
})

test('applyTheme removes prior font links before adding new ones', () => {
  applyTheme({ css: "@import url('https://a.example/x.css');:root{}", bg: '#000000' }, ctx())
  applyTheme({ css: "@import url('https://b.example/y.css');:root{}", bg: '#000000' }, ctx())
  assert.equal(dom.fontLinks.length, 1)
  assert.equal(dom.fontLinks[0].href, 'https://b.example/y.css')
})

test('applyTheme lets the stylesheet own --bg; the bg hint only feeds theme-color', () => {
  // A pre-paint inline --bg would outrank an owner background set in an html
  // rule, a condition or a layer, so applying theme css removes it.
  dom.documentElement.style.setProperty('--bg', '#0d0d0d')
  applyTheme({ css: ':root{}', bg: '#abcdef' }, ctx())
  assert.equal(dom.documentElement.style.getPropertyValue('--bg'), undefined)
  assert.equal(dom.document.body.style.background, 'var(--bg)')
  assert.equal(dom.documentElement.style.background, 'var(--bg)')
  assert.equal(dom.meta.content, '#abcdef', 'no CSSOM in this stub: the hint is the fallback')
})

test('applyTheme reads the cascade background for theme-color and the boot hint', () => {
  // The server hint can be a guess (an html-rule background is invisible to
  // it); what the stylesheet resolves wins.
  dom.document.defaultView = { getComputedStyle: () => ({ getPropertyValue: (n) => (n === '--bg' ? ' #ffffff' : '') }) }
  applyTheme({ css: 'html { --bg: #ffffff; --text: #111111; }', bg: '#0d0d0d', mode: 'light' }, ctx())
  assert.equal(dom.documentElement.style.getPropertyValue('--bg'), undefined)
  assert.equal(dom.meta.content, '#ffffff')
  assert.equal(JSON.parse(store.getItem('mobius-theme')).bg, '#ffffff')
})

test('applyTheme records the background of the mode it switches to, not the previous one', () => {
  // A theme keyed on data-theme resolves a different --bg once the attribute
  // flips; theme-color and the next-boot hint must follow the new mode.
  const cascade = { dark: '#000000', light: '#ffffff' }
  dom.document.defaultView = {
    getComputedStyle: () => ({
      getPropertyValue: (n) => (n === '--bg' ? cascade[dom.documentElement.getAttribute('data-theme')] || '' : ''),
    }),
  }
  dom.documentElement.setAttribute('data-theme', 'dark')
  const css = ':root[data-theme="dark"] { --bg: #000000; } :root[data-theme="light"] { --bg: #ffffff; }'
  applyTheme({ css, bg: '#000000', mode: 'light' }, ctx())
  assert.equal(dom.documentElement.getAttribute('data-theme'), 'light')
  assert.equal(dom.meta.content, '#ffffff')
  assert.deepEqual(JSON.parse(store.getItem('mobius-theme')), { bg: '#ffffff', mode: 'light' })
})

test('applyTheme without css keeps the bg as the only (inline) source', () => {
  applyTheme({ bg: '#abcdef' }, ctx())
  assert.equal(dom.documentElement.style.getPropertyValue('--bg'), '#abcdef')
  assert.equal(dom.document.body.style.background, '#abcdef')
})

test('applyTheme ignores a non-hex bg (no meta / no persist mutation)', () => {
  dom.document.body.style.background = '#init'
  dom.meta.content = '#init'
  applyTheme({ css: ':root{}', bg: 'expression(alert(1))' }, ctx())
  assert.equal(dom.document.body.style.background, 'var(--bg)', 'follows the cascade, never the hint')
  assert.equal(dom.meta.content, '#init')
  assert.equal(store.getItem('mobius-theme'), null)
})

test('applyTheme sets live theme state without rewriting iOS launch policy', () => {
  applyTheme({ css: ':root{--bg:#f0eeeb;}', bg: '#f0eeeb' }, ctx())  // light
  assert.equal(dom.documentElement.getAttribute('data-theme'), 'light')
  assert.equal(dom.documentElement.style.colorScheme, 'light')
  assert.equal(dom.colorScheme.content, 'light dark')
  assert.equal(dom.statusBar.content, 'black')

  applyTheme({ css: ':root{--bg:#0d0d0d;}', bg: '#0d0d0d' }, ctx())  // dark
  assert.equal(dom.documentElement.getAttribute('data-theme'), 'dark')
  assert.equal(dom.documentElement.style.colorScheme, 'dark')
  assert.equal(dom.colorScheme.content, 'dark light')
  assert.equal(dom.statusBar.content, 'black')
})

test('colorSchemeMetaContent orders the active mode first', () => {
  assert.equal(colorSchemeMetaContent('light'), 'light dark')
  assert.equal(colorSchemeMetaContent('dark'), 'dark light')
  assert.equal(colorSchemeMetaContent(undefined), 'dark light')
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

test('applyTheme persists the structured mobius-theme value', () => {
  applyTheme({ css: ':root{--bg:#f0eeeb;}', bg: '#f0eeeb' }, ctx())
  assert.deepEqual(JSON.parse(store.getItem('mobius-theme')), { bg: '#f0eeeb', mode: 'light' })
})

test('applyTheme does NOT persist inside an iframe (window.parent !== window) — the drawer-bleed fix', () => {
  // The opaque app frame receives a memory-only compatibility storage facade.
  // With its empty theme slot it would resolve the dark default and clobber the
  // owner's real theme in the shared key, which the shell re-reads at boot
  // (recoloring the drawer). Only the top-level shell may persist.
  const saved = Object.getOwnPropertyDescriptor(globalThis, 'window')
  try {
    globalThis.window = { parent: {} }              // iframe: parent is the shell, not self
    applyTheme({ css: ':root{--bg:#0d0d0d;}', bg: '#0d0d0d', mode: 'dark' }, ctx())
    assert.equal(store.getItem('mobius-theme'), null, 'iframe must NOT write the shared theme key')
    const top = {}; top.parent = top               // top-level shell: parent === self
    globalThis.window = top
    applyTheme({ css: ':root{--bg:#f0eeeb;}', bg: '#f0eeeb', mode: 'light' }, ctx())
    assert.deepEqual(
      JSON.parse(store.getItem('mobius-theme')),
      { bg: '#f0eeeb', mode: 'light' },
      'top-level shell persists',
    )
  } finally {
    if (saved) Object.defineProperty(globalThis, 'window', saved); else delete globalThis.window
  }
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
  const t = resolveTheme(ctx())
  assert.equal(t.bg, '#f0eeeb')
  assert.equal(t.mode, 'light')
  assert.equal(t.css, undefined)
})

test('resolveTheme: 3) dark default when nothing present', () => {
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


// --- The served theme at the application boundary -------------------------
//
// These run the real appliers over the exact CSS the server serves
// (themeCssCases.json `expected`, pinned to backend _ensure_core_vars).

const { default: themeCssCases } = await import('./themeCssCases.json', { with: { type: 'json' } })
const { INJECTED_DEFAULTS_MARKER, themeDefaultsForCss } = await import('../../theme.js')
const served = (prefix) => themeCssCases.find((c) => c.name.startsWith(prefix)).expected
const GOOGLE_FONTS = 'https://fonts.googleapis.com/css2?family=Inter:wght@400;600&display=swap'
const CORE_VAR_COUNT = Object.keys(themeDefaultsForCss('')).length

function generatedVars(styleText) {
  const at = styleText.indexOf(INJECTED_DEFAULTS_MARKER)
  assert.ok(at >= 0, 'the generated defaults block survived import extraction')
  const block = styleText.slice(at, styleText.indexOf('}\n}\n', at))
  return [...block.matchAll(/(--[\w-]+):/g)].map((m) => m[1])
}

function runPrepaint(slot) {
  dom.slotNodes.set('__mobius-theme__', { textContent: JSON.stringify(slot) })
  const run = new Function('document', 'localStorage', 'location', 'window', PREPAINT_SRC)
  run(dom.document, store, { pathname: '/' }, { parent: null })
}

for (const prefix of [
  'a quoted multi-weight Google Fonts import',
  'a bare multi-weight Google Fonts import',
  'charset, layer order and several imports',
]) {
  test(`applyTheme keeps the import URL whole and the defaults complete: ${prefix}`, () => {
    applyTheme({ css: served(prefix), bg: '#112233' }, ctx())
    assert.ok(dom.fontLinks.some((l) => l.href === GOOGLE_FONTS), dom.fontLinks.map((l) => l.href).join(', '))
    const style = dom.styleNodes.get('mobius-theme').textContent
    assert.equal(style.includes('@import'), false)
    assert.equal(generatedVars(style).length, CORE_VAR_COUNT)
  })

  test(`the pre-paint script extracts the same: ${prefix}`, () => {
    runPrepaint({ css: served(prefix), bg: '#112233', mode: 'dark' })
    assert.ok(dom.fontLinks.some((l) => l.href === GOOGLE_FONTS))
    assert.equal(generatedVars(dom.styleNodes.get('mobius-theme').textContent).length, CORE_VAR_COUNT)
  })
}

function assertEscapedImportApplied() {
  assert.deepEqual(dom.fontLinks.map((l) => l.href), ['https://example.test/a);b.css'])
  const style = dom.styleNodes.get('mobius-theme').textContent
  assert.equal(style.includes('@import'), false)
  assert.equal(style.includes('b.css'), false, 'no fragment of the URL is left as CSS')
  assert.equal(generatedVars(style).length, CORE_VAR_COUNT)
  assert.ok(style.endsWith(':root { --accent: #00ffff; }\n'))
}

test('applyTheme strips a bare import URL with an escaped ) whole and keeps the defaults', () => {
  applyTheme({ css: served('an escaped ) in an unquoted import URL'), bg: '#0d0d0d' }, ctx())
  assertEscapedImportApplied()
})

test('the pre-paint script strips the same escaped import whole', () => {
  runPrepaint({ css: served('an escaped ) in an unquoted import URL'), bg: '#0d0d0d', mode: 'dark' })
  assertEscapedImportApplied()
})

test('pre-paint with theme css leaves --bg to the stylesheet', () => {
  // An html-rule background: the server's bg hint is the generated dark one.
  runPrepaint({ css: served('an html rule outranks'), bg: '#0d0d0d', mode: 'light' })
  assert.equal(dom.documentElement.style.getPropertyValue('--bg'), undefined)
  assert.equal(dom.meta.content, '#0d0d0d', 'theme-color is pre-CSS chrome: the hint is fine there')
})

test('pre-paint without css hints --bg inline until a stylesheet arrives', () => {
  runPrepaint({ bg: '#f0eeeb', mode: 'light' })
  assert.equal(dom.documentElement.style.getPropertyValue('--bg'), '#f0eeeb')
  applyTheme({ css: served('an html rule outranks'), bg: '#0d0d0d', mode: 'light' }, ctx())
  assert.equal(dom.documentElement.style.getPropertyValue('--bg'), undefined)
  assert.equal(dom.document.body.style.background, 'var(--bg)')
})
