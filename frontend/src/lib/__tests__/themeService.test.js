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
  const documentElement = { _attrs: {}, getAttribute: (k) => documentElement._attrs[k], setAttribute: (k, v) => { documentElement._attrs[k] = v } }

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

beforeEach(async () => {
  dom = makeDomStub()
  globalThis.document = dom.document
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
