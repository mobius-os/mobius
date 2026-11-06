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
      setProperty(name, value) { this._props[name] = value }, removeProperty(name) { delete this._props[name] },
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
  applyTheme('', 'expression(alert(1))')
  assert.equal(dom.body.style.background, '#init')
  assert.equal(dom.documentElement.style.getPropertyValue('--bg'), '#init')
})

test('frame live applier hands --bg to the theme stylesheet once css applies', () => {
  // An inline --bg (the pre-paint hint) would outrank an owner background in
  // an html rule, a condition or a layer, so applying css removes it.
  const dom = makeFrameDom()
  dom.documentElement.style.setProperty('--bg', '#0d0d0d')
  const applyTheme = loadLiveApplyTheme(dom)
  applyTheme(':root{}', '#0d0d0d')
  assert.equal(dom.documentElement.style.getPropertyValue('--bg'), undefined)
  assert.equal(dom.body.style.background, 'var(--bg)')
})

test('frame live applier writes a valid hex bg when no css arrives', () => {
  const dom = makeFrameDom()
  const applyTheme = loadLiveApplyTheme(dom)
  applyTheme('', '#abcdef')
  assert.equal(dom.body.style.background, '#abcdef')
  assert.equal(dom.documentElement.style.getPropertyValue('--bg'), '#abcdef')
})

// --- Generated theme defaults versus the frame's static fallbacks ------
//
// The frame's static fallbacks and every served theme's generated defaults
// share the `mobius-defaults` cascade layer, declared first by the static
// <style>, so it is the weakest layer. Within it the theme style (applied
// after) replaces the static values; any owner rule -- unlayered, in an owner
// layer, or on `html` -- beats both. This drives the real inlined applier for
// the initial (`moebius:frame-init`) and live (`moebius:frame-theme`) paths --
// both call applyTheme(themeCss, bg) -- and resolves the root with a small
// layer-aware cascade oracle, then checks no inline --bg overrides it.

const { default: themeCssCases } = await import('./themeCssCases.json', { with: { type: 'json' } })
const { DARK_ROLES, LIGHT_ROLES, contrastRatio } = await import('../../theme.js')

function frameStaticCss() {
  return FRAME_HTML.match(/<style>(\s*\/\* Fallback defaults[\s\S]*?)<\/style>/)[1]
}

// Root custom properties after the cascade for the shapes used here: @layer
// statements and blocks (named or anonymous), @media (skipped: the query is
// taken as false), `:where(:root)`, `:where(:root[data-theme="light"])`,
// `html` and `:root`. Normal declarations: unlayered beats layered, a later
// layer beats an earlier one, then specificity, then source order. Fixtures
// here contain no quoted braces or semicolons.
function resolveRoot(css, dataTheme) {
  const specificity = { ':where(:root)': 0, html: 1, ':root': 10 }
  if (dataTheme === 'light') specificity[':where(:root[data-theme="light"])'] = 0
  const layers = []
  const declare = (name) => { if (!layers.includes(name)) layers.push(name) }
  const decls = []
  let anonymous = 0
  const walk = (text, layer) => {
    let start = 0
    for (let i = 0; i < text.length; i++) {
      if (text[i] === ';') {
        const statement = text.slice(start, i).trim()
        const m = /^@layer\s+(.+)$/s.exec(statement)
        if (m) m[1].split(',').forEach((n) => declare(n.trim()))
        start = i + 1
      } else if (text[i] === '{') {
        let depth = 1
        let j = i + 1
        for (; depth; j++) depth += text[j] === '{' ? 1 : text[j] === '}' ? -1 : 0
        const prelude = text.slice(start, i).trim().replace(/\s+/g, ' ')
        const body = text.slice(i + 1, j - 1)
        if (prelude.startsWith('@layer')) {
          const name = prelude.slice(6).trim() || `<anonymous ${anonymous++}>`
          declare(name)
          walk(body, name)
        } else if (prelude in specificity) {
          for (const part of body.split(';')) {
            const d = /^\s*(--[\w-]+)\s*:([\s\S]*)$/.exec(part)
            if (d) decls.push({ layer, spec: specificity[prelude], order: decls.length, name: d[1], value: d[2].trim() })
          }
        }
        i = j - 1
        start = j
      }
    }
  }
  walk(css.replace(/\/\*[\s\S]*?\*\//g, ''), null)
  const rank = (d) => (d.layer === null ? layers.length : layers.indexOf(d.layer))
  decls.sort((a, b) => rank(a) - rank(b) || a.spec - b.spec || a.order - b.order)
  return Object.fromEntries(decls.map((d) => [d.name, d.value]))
}

function frameRoot(dom, dataTheme) {
  assert.equal(dom.documentElement.style.getPropertyValue('--bg'), undefined,
    'no inline --bg may outrank the stylesheet cascade')
  return resolveRoot(`${frameStaticCss()}\n${dom.styleNodes.get('mobius-theme').textContent}`, dataTheme)
}

const served = (prefix) => themeCssCases.find((c) => c.name.startsWith(prefix)).expected

test('frame: the static fallbacks sit in the defaults layer, declared first', () => {
  const css = frameStaticCss().replace(/\/\*[\s\S]*?\*\//g, '')
  assert.match(css, /^\s*@layer mobius-defaults \{/)
  const root = resolveRoot(css, 'dark')
  assert.equal(root['--accent-text'], DARK_ROLES['--accent-text'])
})

test('frame: owner rules win over generated and static defaults, initially and live', () => {
  const dom = makeFrameDom()
  dom.documentElement.style.setProperty('--bg', '#0d0d0d') // the pre-paint hint
  const applyTheme = loadLiveApplyTheme(dom)

  // frame-init: owner values in a named layer.
  applyTheme(served('a named owner layer'), '#0d0d0d')
  let root = frameRoot(dom, 'dark')
  assert.equal(root['--accent'], '#00ffff')
  assert.equal(root['--text'], '#112233')
  for (const name of ['--bg', '--surface', '--accent-fill', '--warning-text']) assert.ok(root[name], `${name} resolves`)

  // frame-theme live swap: an html-rule background the server hint cannot see.
  applyTheme(served('an html rule outranks'), '#0d0d0d')
  root = frameRoot(dom, 'dark')
  assert.equal(root['--bg'], '#ffffff')
  assert.equal(root['--text'], '#111111')

  // Another live swap: layer-order statement, and a light custom accent
  // against the static light block that carries the shipped purple role.
  applyTheme(served('a layer-order statement'), '#0d0d0d')
  assert.equal(frameRoot(dom, 'dark')['--accent'], '#00ffff')
  applyTheme(served('duplicate declarations in one root'), '#f0eeeb')
  root = frameRoot(dom, 'light')
  assert.equal(root['--accent'], '#0ea5e9')
  assert.notEqual(root['--accent-text'], LIGHT_ROLES['--accent-text'])
  assert.ok(contrastRatio(root['--accent-text'], root['--surface-2']) >= 4.5)
  assert.equal(root['--surface-2'], '#e8e6e2', 'the light default, not the static dark one')
})

test('frame: an escaped ) in a bare import URL is stripped whole and the owner root still wins', () => {
  const dom = makeFrameDom()
  const applyTheme = loadLiveApplyTheme(dom)
  applyTheme(served('an escaped ) in an unquoted import URL'), '#0d0d0d')
  assert.deepEqual(dom.fontLinks.map((l) => l.href), ['https://example.test/a);b.css'])
  const style = dom.styleNodes.get('mobius-theme').textContent
  assert.equal(style.includes('b.css'), false)
  const root = frameRoot(dom, 'dark')
  assert.equal(root['--accent'], '#00ffff')
  assert.ok(root['--accent-fill'] && root['--surface'])
})
