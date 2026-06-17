/**
 * Unit tests for themeService.js.
 *
 * Run with:
 *   cd frontend && node --test src/lib/__tests__/themeService.test.js
 *
 * Covers applyThemeToDom (Commit 2) and persistTheme (Commit 2).
 * toggleTheme + cache-invalidation contract lives in Commit 3a and
 * is asserted in a separate test file added with that commit.
 *
 * We provide a tiny DOM stub instead of pulling in jsdom — the
 * surface applyThemeToDom touches is small enough that the stub
 * is cheaper than the dependency.
 */
import { test, beforeEach } from 'node:test'
import assert from 'node:assert/strict'

// Minimal DOM stub: enough for applyThemeToDom to walk document,
// head, body, and run querySelector / querySelectorAll / createElement.
function makeDomStub() {
  const styleNodes = new Map()  // id -> node
  const fontLinks = []
  const headChildren = []
  const meta = { content: '#000000', getAttribute: (k) => meta[k], setAttribute: (k, v) => { meta[k] = v } }
  // Status-bar meta only resolves on PWA/iOS. Stub it as present so
  // themeService's `if (sb) sb.setAttribute(...)` exercises the path
  // (and the stub captures the assignment for the assertions below).
  const statusBar = { content: 'black', getAttribute: (k) => statusBar[k], setAttribute: (k, v) => { statusBar[k] = v } }
  const body = { style: {} }
  // documentElement was added when light/dark mode support landed (set
  // data-theme attribute drives the CSS variable cascade). The stub
  // captures the assignment so tests can assert mode inference.
  // `.style` captures inline custom-property writes (documentElement.style
  // .setProperty('--bg', ...)) so the BUG-2 inline-sync assertions can read
  // them back. `_props` records the last setProperty per name.
  const documentElement = {
    _attrs: {},
    getAttribute: (k) => documentElement._attrs[k],
    setAttribute: (k, v) => { documentElement._attrs[k] = v },
    style: { _props: {}, setProperty(name, value) { this._props[name] = value }, getPropertyValue(name) { return this._props[name] } },
  }

  function makeNode(tag) {
    const node = {
      tagName: tag.toUpperCase(),
      id: '',
      rel: '',
      href: '',
      textContent: '',
      dataset: {},
      _parent: null,
      remove() {
        if (this.tagName === 'STYLE') styleNodes.delete(this.id)
        if (this.tagName === 'LINK') {
          const i = fontLinks.indexOf(this); if (i >= 0) fontLinks.splice(i, 1)
          const j = headChildren.indexOf(this); if (j >= 0) headChildren.splice(j, 1)
        }
      },
    }
    return node
  }

  return {
    document: {
      createElement(tag) { return makeNode(tag) },
      getElementById(id) { return styleNodes.get(id) || null },
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
          if (node.tagName === 'STYLE' && node.id) {
            styleNodes.set(node.id, node)
          }
          if (node.tagName === 'LINK' && node.dataset.themeFont) {
            fontLinks.push(node)
          }
          const i = headChildren.indexOf(node)
          if (i >= 0) headChildren.splice(i, 1)
          headChildren.push(node)
          node._parent = this
          return node
        },
        get children() { return headChildren },
      },
      body,
      documentElement,
    },
    meta,
    statusBar,
    documentElement,
    fontLinks,
    headChildren,
    styleNodes,
  }
}

let dom
let themeService

// Minimal localStorage stub — applyThemeToDom now persists the active
// bg to localStorage['mobius-theme-bg'] so the next cold-boot splash
// reads the current theme's bg (BUG 2). Captures the writes for assertion.
function makeLocalStorageStub() {
  const map = new Map()
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => { map.set(k, String(v)) },
    removeItem: (k) => { map.delete(k) },
  }
}

beforeEach(async () => {
  dom = makeDomStub()
  globalThis.document = dom.document
  globalThis.localStorage = makeLocalStorageStub()
  // Bust ESM cache so the import is fresh per test.
  const url = new URL('../themeService.js', import.meta.url).href
    + `?t=${Math.random()}`
  themeService = await import(url)
})

test('applyThemeToDom injects CSS into a <style id="mobius-theme"> node', () => {
  themeService.applyThemeToDom(':root { --bg: #123456; }', '#123456')
  const style = dom.styleNodes.get('mobius-theme')
  assert.ok(style, 'style node should be created')
  assert.equal(style.textContent, ':root { --bg: #123456; }')
})

test('applyThemeToDom re-appends the style node so it wins the cascade', () => {
  // First call creates the node.
  themeService.applyThemeToDom(':root { --bg: #111111; }', '#111111')
  const firstNode = dom.styleNodes.get('mobius-theme')
  // A foreign node sneaks in (server-injected initial theme block).
  const intruder = dom.document.createElement('style')
  intruder.id = 'intruder'
  dom.document.head.appendChild(intruder)
  // Second call must move the mobius-theme node to the END of head.
  themeService.applyThemeToDom(':root { --bg: #222222; }', '#222222')
  const order = dom.headChildren
  assert.equal(order[order.length - 1], firstNode,
    'mobius-theme must be the LAST head child after re-apply')
  assert.equal(firstNode.textContent, ':root { --bg: #222222; }')
})

test('applyThemeToDom strips @import lines from the CSS body', () => {
  const css = `@import url('https://fonts.example.com/inter.css');\n:root { --bg: #abc; }`
  themeService.applyThemeToDom(css, '#aabbcc')
  const style = dom.styleNodes.get('mobius-theme')
  assert.ok(!style.textContent.includes('@import'),
    'inline @import must be stripped from <style>')
  assert.ok(style.textContent.includes('--bg: #abc'))
})

test('applyThemeToDom converts safe @import urls into <link> tags', () => {
  const css = `@import url('https://fonts.example.com/inter.css');\n:root {}`
  themeService.applyThemeToDom(css, '#000000')
  assert.equal(dom.fontLinks.length, 1)
  assert.equal(dom.fontLinks[0].href, 'https://fonts.example.com/inter.css')
  assert.equal(dom.fontLinks[0].rel, 'stylesheet')
  assert.equal(dom.fontLinks[0].dataset.themeFont, '1')
})

test('applyThemeToDom rejects javascript:/data: @import URLs', () => {
  // Mirrors the server-side allowlist (theme.py:_is_safe_import_url)
  // so a `javascript:` URL slipped into theme.css can't become a
  // stylesheet link on the client path either.
  const css = `@import url('javascript:alert(1)');@import url('data:text/css;base64,x');:root {}`
  themeService.applyThemeToDom(css, '#000000')
  assert.equal(dom.fontLinks.length, 0)
})

test('applyThemeToDom removes prior font links before adding new ones', () => {
  themeService.applyThemeToDom(`@import url('https://a.example/x.css');:root {}`, '#000')
  themeService.applyThemeToDom(`@import url('https://b.example/y.css');:root {}`, '#000')
  assert.equal(dom.fontLinks.length, 1, 'stale link should be removed')
  assert.equal(dom.fontLinks[0].href, 'https://b.example/y.css')
})

test('applyThemeToDom updates body.style.background when bg is valid hex', () => {
  themeService.applyThemeToDom(':root {}', '#abcdef')
  assert.equal(dom.document.body.style.background, '#abcdef')
})

test('applyThemeToDom updates <meta name="theme-color"> when bg is valid hex', () => {
  themeService.applyThemeToDom(':root {}', '#fedcba')
  assert.equal(dom.meta.content, '#fedcba')
})

test('applyThemeToDom ignores non-hex bg', () => {
  // Defensive: server-side regex constrains bg, but client must not
  // blindly trust the wire either. If bg is malformed (e.g. a CSS
  // expression slipped through), neither body nor meta should mutate.
  dom.document.body.style.background = '#initial'
  dom.meta.content = '#initial'
  themeService.applyThemeToDom(':root {}', 'expression(alert(1))')
  assert.equal(dom.document.body.style.background, '#initial')
  assert.equal(dom.meta.content, '#initial')
})

test('applyThemeToDom is idempotent — repeated calls produce identical DOM', () => {
  const css = `@import url('https://fonts.example.com/inter.css');\n:root { --bg: #abc; }`
  themeService.applyThemeToDom(css, '#aabbcc')
  themeService.applyThemeToDom(css, '#aabbcc')
  assert.equal(dom.fontLinks.length, 1, 'no duplicate font links')
  assert.equal(dom.styleNodes.size, 1, 'one style node')
})

test('persistTheme writes CSS + mode + sends notify in parallel', async () => {
  const calls = []
  const api = {
    storage: { shared: {
      putThemeCss: (content) => { calls.push(['putThemeCss', content]); return Promise.resolve() },
      putThemeMode: (mode) => { calls.push(['putThemeMode', mode]); return Promise.resolve() },
    }},
    notify: { send: (payload) => { calls.push(['notify', payload]); return Promise.resolve() } },
  }
  await themeService.persistTheme(':root {}', 'dark', api)
  // Both PUTs must happen.
  assert.ok(calls.some(c => c[0] === 'putThemeCss' && c[1] === ':root {}'))
  assert.ok(calls.some(c => c[0] === 'putThemeMode' && c[1] === 'dark'))
  assert.ok(calls.some(c => c[0] === 'notify'
    && c[1].type === 'theme_updated'))
})

test('persistTheme throws when a PUT fails (caller does rollback)', async () => {
  // SettingsView's catch block hinges on this throw. Swallowing
  // the rejection would let the optimistic UI persist while
  // server-side state lagged behind.
  const api = {
    storage: { shared: {
      putThemeCss: () => Promise.reject(new Error('500')),
      putThemeMode: () => Promise.resolve(),
    }},
    notify: { send: () => Promise.resolve() },
  }
  await assert.rejects(
    themeService.persistTheme(':root {}', 'dark', api),
    /500/,
  )
})

test('persistTheme swallows notify.send failures (best-effort)', async () => {
  // /notify is a best-effort SSE side-channel. A failing notify
  // must not roll back the user's theme — they already see the
  // persisted state.
  const api = {
    storage: { shared: {
      putThemeCss: () => Promise.resolve(),
      putThemeMode: () => Promise.resolve(),
    }},
    notify: { send: () => Promise.reject(new Error('network')) },
  }
  // Should not throw.
  await themeService.persistTheme(':root {}', 'light', api)
})

test('getEffectiveTheme reads back the applied LIGHT theme (offline-safe iframe source)', () => {
  themeService.applyThemeToDom(':root { --bg: #f0eeeb; }', '#f0eeeb')
  const eff = themeService.getEffectiveTheme()
  assert.ok(eff, 'returns the applied theme')
  assert.equal(eff.css, ':root { --bg: #f0eeeb; }')
  assert.equal(eff.bg, '#f0eeeb')
  assert.equal(eff.mode, 'light')
})

test('getEffectiveTheme reflects a DARK theme via data-theme', () => {
  themeService.applyThemeToDom(':root { --bg: #0d0d0d; }', '#0d0d0d')
  const eff = themeService.getEffectiveTheme()
  assert.equal(eff.bg, '#0d0d0d')
  assert.equal(eff.mode, 'dark')
})

test('getEffectiveTheme returns null when no theme has been applied', () => {
  assert.equal(themeService.getEffectiveTheme(), null)
})


// --- BUG 2: applyThemeToDom syncs the inline --bg + localStorage ---
// The splash script in index.html sets an INLINE --bg on <html> from
// localStorage['mobius-theme-bg'] before first paint. An inline style on
// documentElement beats `:root{}`, so applyThemeToDom must overwrite it
// (and the localStorage key) with the theme it actually paints — otherwise
// a stale splash value pins the wrong background after a toggle/reload.

test('applyThemeToDom syncs the inline --bg on <html> to the active bg', () => {
  // Seed a STALE inline value the splash would have written (dark).
  dom.documentElement.style.setProperty('--bg', '#0d0d0d')
  themeService.applyThemeToDom(':root { --bg: #f0eeeb; }', '#f0eeeb')
  assert.equal(dom.documentElement.style.getPropertyValue('--bg'), '#f0eeeb',
    'inline --bg on <html> must track the painted theme, not the stale splash value')
})

test('applyThemeToDom writes localStorage[mobius-theme-bg] to the active bg', () => {
  localStorage.setItem('mobius-theme-bg', '#0d0d0d')  // stale dark from a prior splash
  themeService.applyThemeToDom(':root { --bg: #f0eeeb; }', '#f0eeeb')
  assert.equal(localStorage.getItem('mobius-theme-bg'), '#f0eeeb',
    'next cold-boot splash must read the CURRENT bg, not the previous one')
})

test('applyThemeToDom does NOT touch inline --bg or localStorage for a non-hex bg', () => {
  // Defensive: a malformed bg must not corrupt the inline var or the
  // persisted splash key (same gate as body/meta).
  dom.documentElement.style.setProperty('--bg', '#initial')
  localStorage.setItem('mobius-theme-bg', '#initial')
  themeService.applyThemeToDom(':root {}', 'expression(alert(1))')
  assert.equal(dom.documentElement.style.getPropertyValue('--bg'), '#initial')
  assert.equal(localStorage.getItem('mobius-theme-bg'), '#initial')
})

test('applyThemeToDom dark→light then light→dark leaves a consistent inline --bg', () => {
  // The owner's exact scenario in miniature: repeated toggles must keep
  // the inline --bg in lockstep with the painted theme each time.
  themeService.applyThemeToDom(':root { --bg: #f0eeeb; }', '#f0eeeb')  // light
  assert.equal(dom.documentElement.style.getPropertyValue('--bg'), '#f0eeeb')
  themeService.applyThemeToDom(':root { --bg: #0d0d0d; }', '#0d0d0d')  // dark
  assert.equal(dom.documentElement.style.getPropertyValue('--bg'), '#0d0d0d')
  themeService.applyThemeToDom(':root { --bg: #f0eeeb; }', '#f0eeeb')  // back to light
  assert.equal(dom.documentElement.style.getPropertyValue('--bg'), '#f0eeeb')
  assert.equal(localStorage.getItem('mobius-theme-bg'), '#f0eeeb')
})
