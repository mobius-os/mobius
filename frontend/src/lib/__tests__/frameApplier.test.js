/**
 * Pins the SECURITY behavior of the frame's INLINED live applyTheme(css,bg)
 * in public/app-frame.html — the postMessage live-swap half that can't import
 * src/lib/applyTheme.js, so it inlines an equivalent that must stay in sync.
 *
 * Two ways the inlined applier could regress the security boundary the shared
 * library enforces:
 *   1. A `javascript:`/`data:` `@import` becoming active CSS / a <link>
 *      (the library converts ONLY http(s) imports to <link data-theme-font>).
 *   2. A non-hex `bg` (a CSS expression) reaching body.style.background / --bg
 *      (the library gates every bg write on HEX_RE).
 *
 * We extract the `inferMode` + `applyTheme` function bodies out of the static
 * HTML (same trick as applyTheme.prepaint.test.js extracts the pre-paint IIFE)
 * and eval them against a tiny DOM stub, so this test breaks if someone edits
 * the live applier in a way that re-opens either hole.
 *
 * Run with:
 *   cd frontend && node --test src/lib/__tests__/frameApplier.test.js
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const frontendRoot = join(here, '..', '..', '..')
const FRAME_HTML = readFileSync(join(frontendRoot, 'public', 'app-frame.html'), 'utf8')

/**
 * Pull a top-level `function NAME(...) { ... }` body out of the frame HTML by
 * matching the opening line and walking braces to the matching close. The
 * inlined applier indents its functions with 4 spaces inside <script>.
 */
function extractFunction(html, name) {
  const startRe = new RegExp(`\\n( *)function ${name}\\(`)
  const m = html.match(startRe)
  assert.ok(m, `function ${name} not found in app-frame.html`)
  const indent = m[1]
  const open = html.indexOf('{', m.index)
  let depth = 0
  let i = open
  for (; i < html.length; i++) {
    if (html[i] === '{') depth++
    else if (html[i] === '}') { depth--; if (depth === 0) break }
  }
  const src = `function ${name}` + html.slice(html.indexOf('(', m.index), i + 1)
  return src.replace(new RegExp(`^${indent}`, 'gm'), '')
}

// --- Source-grep guards (cheap, catch a wholesale rewrite) -------------

test('frame live applier source keeps the HEX check', () => {
  assert.match(FRAME_HTML, /var HEX = \/\^#\[0-9a-fA-F\]\{3,8\}\$\//)
  assert.match(FRAME_HTML, /HEX\.test\(bg\)/)
})

test('frame live applier source keeps the http(s) @import allowlist', () => {
  assert.match(FRAME_HTML, /\/\^https\?:\\?\/\\?\/\/i\.test\(fontUrls/)
  // The regex now opens a non-capturing alternation over all four @import
  // spellings (url()/bare/no-url()), so `url` no longer follows `@import\s+`
  // directly — guard the broadened opener instead.
  assert.match(FRAME_HTML, /@import\\s\+\(\?:url/)
})

// --- Behavioral test: extract + eval the live applier ------------------

function makeFrameDom() {
  const fontLinks = []
  const headChildren = []
  const styleNodes = new Map()
  const body = { style: {} }
  const documentElement = {
    _attrs: {},
    getAttribute(k) { return this._attrs[k] },
    setAttribute(k, v) { this._attrs[k] = v },
    style: {
      colorScheme: '',
      _props: {},
      setProperty(name, value) { this._props[name] = value },
      getPropertyValue(name) { return this._props[name] },
    },
  }
  function makeNode(tag) {
    return {
      tagName: tag.toUpperCase(),
      id: '', rel: '', href: '', textContent: '',
      setAttribute(k, v) { this[k === 'data-theme-font' ? '_themeFont' : k] = v },
      remove() {
        const i = fontLinks.indexOf(this); if (i >= 0) fontLinks.splice(i, 1)
        const j = headChildren.indexOf(this); if (j >= 0) headChildren.splice(j, 1)
      },
    }
  }
  const document = {
    createElement(tag) { return makeNode(tag) },
    getElementById(id) { return styleNodes.get(id) || null },
    querySelectorAll(sel) {
      if (sel.includes('data-theme-font')) return fontLinks.slice()
      return []
    },
    head: {
      appendChild(node) {
        if (node.tagName === 'STYLE' && node.id) styleNodes.set(node.id, node)
        if (node.tagName === 'LINK' && node._themeFont) fontLinks.push(node)
        headChildren.push(node)
        return node
      },
    },
    body,
    documentElement,
  }
  return { document, body, documentElement, fontLinks, styleNodes }
}

function loadLiveApplyTheme(dom) {
  const inferSrc = extractFunction(FRAME_HTML, 'inferMode')
  const applySrc = extractFunction(FRAME_HTML, 'applyTheme')
  // `var HEX = /.../;` lives at module scope in the inlined script (just above
  // applyTheme), so capture it the same way the applier reads it.
  const hexLine = FRAME_HTML.match(/var HEX = \/\^#\[0-9a-fA-F\]\{3,8\}\$\/;/)
  assert.ok(hexLine, 'HEX declaration not found in app-frame.html')
  const factory = new Function('document', `${hexLine[0]}\n${inferSrc}\n${applySrc}\nreturn applyTheme;`)
  return factory(dom.document)
}

test('frame live applier rejects javascript:/data: @import (no <link>)', () => {
  const dom = makeFrameDom()
  const applyTheme = loadLiveApplyTheme(dom)
  applyTheme("@import url('javascript:alert(1)');@import url('data:text/css,x');:root{--bg:#000000;}", '#000000')
  assert.equal(dom.fontLinks.length, 0, 'no <link> for javascript:/data: imports')
  assert.ok(!dom.styleNodes.get('mobius-theme').textContent.includes('@import'))
})

test('frame live applier converts an http(s) @import to a <link>', () => {
  const dom = makeFrameDom()
  const applyTheme = loadLiveApplyTheme(dom)
  applyTheme("@import url('https://fonts.example/x.css');:root{--bg:#000000;}", '#000000')
  assert.equal(dom.fontLinks.length, 1)
  assert.equal(dom.fontLinks[0].href, 'https://fonts.example/x.css')
})

// The broadened regex must cover every @import spelling — bare url(), no-url()
// "X", quoted url() — and still gate each on the http(s) allowlist.
test('frame live applier allowlists a bare url() @import', () => {
  const dom = makeFrameDom()
  const applyTheme = loadLiveApplyTheme(dom)
  applyTheme('@import url(https://a.example/x.css);:root{--bg:#000000;}', '#000000')
  assert.equal(dom.fontLinks.length, 1)
  assert.equal(dom.fontLinks[0].href, 'https://a.example/x.css')
  assert.ok(!dom.styleNodes.get('mobius-theme').textContent.includes('@import'))
})

test('frame live applier allowlists a no-url() quoted @import', () => {
  const dom = makeFrameDom()
  const applyTheme = loadLiveApplyTheme(dom)
  applyTheme('@import "https://b.example/y.css";:root{--bg:#000000;}', '#000000')
  assert.equal(dom.fontLinks.length, 1)
  assert.equal(dom.fontLinks[0].href, 'https://b.example/y.css')
  assert.ok(!dom.styleNodes.get('mobius-theme').textContent.includes('@import'))
})

test('frame live applier drops a bare url(data:) @import (no <link>)', () => {
  const dom = makeFrameDom()
  const applyTheme = loadLiveApplyTheme(dom)
  applyTheme('@import url(data:text/css,x);:root{--bg:#000000;}', '#000000')
  assert.equal(dom.fontLinks.length, 0)
  assert.ok(!dom.styleNodes.get('mobius-theme').textContent.includes('@import'))
})

test('frame live applier drops a no-url() "javascript:" @import (no <link>)', () => {
  const dom = makeFrameDom()
  const applyTheme = loadLiveApplyTheme(dom)
  applyTheme('@import "javascript:alert(1)";:root{--bg:#000000;}', '#000000')
  assert.equal(dom.fontLinks.length, 0)
  assert.ok(!dom.styleNodes.get('mobius-theme').textContent.includes('@import'))
})

test('frame live applier ignores a non-hex bg', () => {
  const dom = makeFrameDom()
  dom.body.style.background = '#init'
  dom.documentElement.style.setProperty('--bg', '#init')
  const applyTheme = loadLiveApplyTheme(dom)
  applyTheme(':root{}', 'expression(alert(1))')
  assert.equal(dom.body.style.background, '#init')
  assert.equal(dom.documentElement.style.getPropertyValue('--bg'), '#init')
})

test('frame live applier writes a valid hex bg', () => {
  const dom = makeFrameDom()
  const applyTheme = loadLiveApplyTheme(dom)
  applyTheme(':root{}', '#abcdef')
  assert.equal(dom.body.style.background, '#abcdef')
  assert.equal(dom.documentElement.style.getPropertyValue('--bg'), '#abcdef')
})
