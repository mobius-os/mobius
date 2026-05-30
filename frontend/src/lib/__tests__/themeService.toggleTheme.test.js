/**
 * Unit tests for themeService.toggleTheme (Commit 3a).
 *
 * These are the SHIP-BLOCKER tests from the v2 design:
 *   - Fix 1: toggleTheme MUST invalidate BOTH themeQueries.invalidate
 *            AND themeQueries.mode.invalidate, otherwise AppCanvas's
 *            iframe theme propagation silently breaks.
 *   - Fix 2: toggleTheme MUST extract `newBg` from the BUILT CSS,
 *            not from the parsed-input meta.
 *
 * Plus a unit assertion that the cache invalidation IS what triggers
 * AppCanvas's useEffect path. AppCanvas reads
 * `useQuery({ queryKey: themeQueryKey })`; its useEffect on
 * [theme?.css, theme?.bg] postMessages the iframe. We simulate that
 * effect with a mock queryClient that captures invalidations + a
 * mock useEffect-like callback that fires the postMessage, and
 * assert the call lands.
 *
 * Run with:
 *   cd frontend && node --test src/lib/__tests__/themeService.toggleTheme.test.js
 */
import { test, beforeEach } from 'node:test'
import assert from 'node:assert/strict'

// Same minimal DOM stub as themeService.test.js — toggleTheme calls
// applyThemeToDom internally, which needs it.
function makeDomStub() {
  const styleNodes = new Map()
  const fontLinks = []
  const headChildren = []
  const meta = { content: '#000000', getAttribute: (k) => meta[k], setAttribute: (k, v) => { meta[k] = v } }
  // Light/dark mode support added documentElement.setAttribute and the
  // status-bar meta lookup — same drift fix as themeService.test.js.
  const statusBar = { content: 'black', getAttribute: (k) => statusBar[k], setAttribute: (k, v) => { statusBar[k] = v } }
  const documentElement = { _attrs: {}, getAttribute: (k) => documentElement._attrs[k], setAttribute: (k, v) => { documentElement._attrs[k] = v } }
  const body = { style: {} }
  function makeNode(tag) {
    return {
      tagName: tag.toUpperCase(),
      id: '', rel: '', href: '', textContent: '', dataset: {},
      remove() {
        if (this.tagName === 'STYLE') styleNodes.delete(this.id)
        if (this.tagName === 'LINK') {
          const i = fontLinks.indexOf(this); if (i >= 0) fontLinks.splice(i, 1)
        }
      },
    }
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
      querySelectorAll(sel) { return sel.includes('data-theme-font') ? fontLinks.slice() : [] },
      head: {
        appendChild(node) {
          if (node.tagName === 'STYLE' && node.id) styleNodes.set(node.id, node)
          if (node.tagName === 'LINK' && node.dataset.themeFont) fontLinks.push(node)
          const i = headChildren.indexOf(node); if (i >= 0) headChildren.splice(i, 1)
          headChildren.push(node)
          return node
        },
      },
      body,
      documentElement,
    },
    meta, statusBar, documentElement, fontLinks, styleNodes, headChildren,
  }
}

// Mock queryClient — records every invalidateQueries call so we can
// assert BOTH theme keys were invalidated.
function makeQueryClient() {
  const invalidated = []
  return {
    invalidated,
    invalidateQueries: (opts) => {
      invalidated.push(opts.queryKey)
      return Promise.resolve()
    },
  }
}

// Build a sample dark-mode theme.css the way the agent or
// /api/theme would emit. Same shape buildThemeCss produces.
const DARK_CSS = `:root {
  --bg: #0d0f14;
  --surface: #151820;
  --surface2: #1c2028;
  --border: #2a2f3a;
  --border-light: #1e2330;
  --text: #d8d8dc;
  --muted: #6b6b76;
  --accent: #ff00ff;
  --font: 'Inter', sans-serif;
  --mono: 'JetBrains Mono', monospace;
  font-size: 15px;
  color-scheme: dark;
}
`

function makeApi(initialCss = DARK_CSS) {
  const calls = []
  let currentCss = initialCss
  return {
    calls,
    storage: { shared: {
      getThemeCss: () => Promise.resolve({
        ok: true,
        text: () => Promise.resolve(currentCss),
      }),
      putThemeCss: (content) => {
        calls.push(['putThemeCss', content])
        currentCss = content
        return Promise.resolve()
      },
      putThemeMode: (mode) => {
        calls.push(['putThemeMode', mode])
        return Promise.resolve()
      },
    }},
    notify: {
      send: (payload) => {
        calls.push(['notify', payload])
        return Promise.resolve()
      },
    },
  }
}

let dom
let themeService

beforeEach(async () => {
  dom = makeDomStub()
  globalThis.document = dom.document
  const url = new URL('../themeService.js', import.meta.url).href
    + `?t=${Math.random()}`
  themeService = await import(url)
})

test('SHIP-BLOCKER #1 — toggleTheme invalidates BOTH theme query keys', async () => {
  // This is the test that locks in the iframe propagation contract.
  // AppCanvas.jsx subscribes to the `['theme']` queryKey; if
  // toggleTheme forgets to invalidate, the iframe stays on the
  // OLD theme until next mount.
  const qc = makeQueryClient()
  const api = makeApi(DARK_CSS)
  await themeService.toggleTheme(qc, 'dark', api)

  // Both keys must be in the invalidated list.
  const keyStrs = qc.invalidated.map(k => JSON.stringify(k))
  assert.ok(keyStrs.includes('["theme"]'),
    `themeQueries.invalidate (key ['theme']) MUST fire — without it AppCanvas iframes stale-theme. Got: ${keyStrs.join(', ')}`)
  assert.ok(keyStrs.includes('["theme-mode"]'),
    `themeQueries.mode.invalidate (key ['theme-mode']) MUST fire — SettingsView seeds lightMode from this. Got: ${keyStrs.join(', ')}`)
})

test('SHIP-BLOCKER #2 — newBg is extracted from BUILT css, not stale meta', async () => {
  // Dark → light toggle. The OLD meta has --bg: #0d0f14 (dark).
  // The BUILT css for light mode has --bg: #f0eeeb. The returned
  // newBg must reflect the BUILT value; passing the stale dark bg
  // to applyThemeToDom would leave body.background + meta theme-
  // color pointing at the old dark color while the <style> block
  // shows light mode.
  const qc = makeQueryClient()
  const api = makeApi(DARK_CSS)
  const result = await themeService.toggleTheme(qc, 'dark', api)
  assert.equal(result.newMode, 'light')
  assert.equal(result.newBg, '#f0eeeb',
    `newBg must be the NEW light bg (#f0eeeb), not the old dark bg. Got ${result.newBg}.`)
  // And the DOM must reflect the new bg too.
  assert.equal(dom.document.body.style.background, '#f0eeeb')
  assert.equal(dom.meta.content, '#f0eeeb')
})

test('toggleTheme persists css + mode before invalidating', async () => {
  // Order matters: if invalidation fires before persist resolves,
  // a refetch could read the OLD css from the server and apply
  // it back, undoing the toggle.
  const events = []
  const qc = {
    invalidated: [],
    invalidateQueries: (opts) => {
      events.push(['invalidate', JSON.stringify(opts.queryKey)])
      qc.invalidated.push(opts.queryKey)
      return Promise.resolve()
    },
  }
  const api = makeApi(DARK_CSS)
  const wrappedPutCss = api.storage.shared.putThemeCss
  api.storage.shared.putThemeCss = async (c) => {
    events.push(['putThemeCss', c.length])
    return wrappedPutCss(c)
  }
  const wrappedPutMode = api.storage.shared.putThemeMode
  api.storage.shared.putThemeMode = async (m) => {
    events.push(['putThemeMode', m])
    return wrappedPutMode(m)
  }
  await themeService.toggleTheme(qc, 'dark', api)
  const order = events.map(e => e[0])
  const putIdx = order.findIndex(e => e === 'putThemeCss')
  const invalidateIdx = order.findIndex(e => e === 'invalidate')
  assert.ok(putIdx >= 0 && invalidateIdx >= 0)
  assert.ok(putIdx < invalidateIdx,
    `Persist must complete before invalidation; got order: ${order.join(' -> ')}`)
})

test('toggleTheme dark → light swaps structural colors but preserves accent', async () => {
  // The user (or the agent) may have set a custom accent. The
  // mode toggle must not clobber it.
  const customCss = DARK_CSS.replace('--accent: #ff00ff', '--accent: #abcdef')
  const qc = makeQueryClient()
  const api = makeApi(customCss)
  await themeService.toggleTheme(qc, 'dark', api)
  const putCall = api.calls.find(c => c[0] === 'putThemeCss')
  assert.ok(putCall, 'putThemeCss must have been called')
  const newCss = putCall[1]
  assert.ok(newCss.includes('--bg: #f0eeeb'), 'structural --bg must swap to light')
  assert.ok(newCss.includes('--accent: #abcdef'),
    'custom --accent must survive the mode toggle')
})

test('toggleTheme returns {newMode, newCss, newBg} for caller convenience', async () => {
  const qc = makeQueryClient()
  const api = makeApi(DARK_CSS)
  const result = await themeService.toggleTheme(qc, 'dark', api)
  assert.equal(result.newMode, 'light')
  assert.ok(typeof result.newCss === 'string' && result.newCss.length > 0)
  assert.equal(result.newBg, '#f0eeeb')
})

test('toggleTheme light → dark works the same way', async () => {
  const LIGHT_CSS = DARK_CSS
    .replace('--bg: #0d0f14', '--bg: #f0eeeb')
    .replace('color-scheme: dark', 'color-scheme: light')
  const qc = makeQueryClient()
  const api = makeApi(LIGHT_CSS)
  const result = await themeService.toggleTheme(qc, 'light', api)
  assert.equal(result.newMode, 'dark')
  // DARK_COLORS.--bg in src/theme.js is the authoritative dark default;
  // kept in sync with backend/app/theme.py DEFAULT_THEME. Was #0d0f14
  // in early 2026-05; rolled to #0d0d0d when the design refresh
  // tightened the neutrals against the lighter --surface stack.
  assert.equal(result.newBg, '#0d0d0d')
})

test('toggleTheme throws when persist fails (caller does rollback)', async () => {
  const qc = makeQueryClient()
  const api = makeApi(DARK_CSS)
  api.storage.shared.putThemeCss = () => Promise.reject(new Error('500'))
  await assert.rejects(
    themeService.toggleTheme(qc, 'dark', api),
    /500/,
  )
  // Invalidation must NOT have fired on the failure path — a
  // cache invalidate after a failed write would refetch the OLD
  // css and clobber whatever optimistic state the caller is
  // about to roll back.
  assert.equal(qc.invalidated.length, 0,
    'cache must not be invalidated when persist throws')
})

test('iframe-propagation contract — invalidation triggers AppCanvas-style postMessage', async () => {
  // Mandatory smoke from commit 3a: verify that the cache
  // invalidation IS what AppCanvas's useEffect uses to trigger
  // the moebius:frame-theme postMessage. We can't run the real
  // AppCanvas under node:test (no React renderer), but we can
  // mock its observable contract: subscribers to ['theme']
  // invalidation get the new theme and call postMessage.
  const qc = makeQueryClient()
  const api = makeApi(DARK_CSS)
  const postedMessages = []

  // Simulate AppCanvas's useQuery subscriber + useEffect on
  // [theme?.css, theme?.bg].
  const origInvalidate = qc.invalidateQueries
  qc.invalidateQueries = async (opts) => {
    const r = await origInvalidate(opts)
    if (JSON.stringify(opts.queryKey) === '["theme"]') {
      // Stand-in for AppCanvas's effect: after invalidation, the
      // (mocked) theme query "refetches" and the effect posts to
      // the iframe with the new css + bg.
      postedMessages.push({
        type: 'moebius:frame-theme',
        themeCss: '<new-css>',
        bg: '<new-bg>',
      })
    }
    return r
  }

  await themeService.toggleTheme(qc, 'dark', api)
  assert.equal(postedMessages.length, 1,
    'AppCanvas-style subscriber must observe the ["theme"] invalidation and postMessage exactly once')
  assert.equal(postedMessages[0].type, 'moebius:frame-theme')
})
