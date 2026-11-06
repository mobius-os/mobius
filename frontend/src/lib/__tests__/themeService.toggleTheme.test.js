/**
 * Unit tests for themeService.toggleTheme.
 *
 * The server builds every theme; the shell only paints. The cached
 * `/api/theme` response carries `toggle`, the theme the switch will save, so
 * the tap paints it at once, and `POST /api/theme/mode` returns what the
 * server then serves. What the switch writes into theme.css is tested on the
 * server (backend/tests/test_theme_toggle.py).
 *
 * Contracts held here: the paint lands before the save resolves; both theme
 * query keys are seeded (AppCanvas posts `moebius:frame-theme` from the
 * ['theme'] seed) and then marked stale without a refetch; a failed save
 * restores the previous paint and throws for the caller's rollback.
 *
 * Run with:
 *   cd frontend && node --test src/lib/__tests__/themeService.toggleTheme.test.js
 */
import { test, beforeEach } from 'node:test'
import assert from 'node:assert/strict'
import { QueryClient } from '@tanstack/query-core'

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
  const documentElement = {
    _attrs: {},
    _classes: new Set(),
    getAttribute: (k) => documentElement._attrs[k],
    setAttribute: (k, v) => { documentElement._attrs[k] = v },
    classList: {
      add: (...names) => names.forEach(name => documentElement._classes.add(name)),
      remove: (...names) => names.forEach(name => documentElement._classes.delete(name)),
      contains: (name) => documentElement._classes.has(name),
    },
    // applyThemeToDom now syncs the inline --bg (BUG 2); capture it.
    style: { _props: {}, setProperty(name, value) { this._props[name] = value }, removeProperty(name) { delete this._props[name] }, getPropertyValue(name) { return this._props[name] } },
  }
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

// Mock queryClient over one cached ['theme'] value; records seeds and
// invalidations.
function makeQueryClient(seedCache) {
  const invalidated = []
  const setData = []
  const events = []
  let cache = seedCache
  return {
    invalidated,
    setData,
    events,
    getQueryData: () => cache,
    setQueryData: (queryKey, value) => {
      events.push('setQueryData')
      setData.push([queryKey, value])
      if (JSON.stringify(queryKey) === '["theme"]') cache = value
      return value
    },
    invalidateQueries: (opts) => {
      events.push('invalidate')
      invalidated.push(opts)
      return Promise.resolve()
    },
  }
}

// Server-shaped themes. A real `css` is the server's effective theme; these
// stand-ins only need distinct bytes and a --bg.
const theme = (mode, bg, extra = '') => ({ css: `:root { --bg: ${bg}; }${extra}`, bg, mode })
const DARK = theme('dark', '#0d0d0d')
const LIGHT = theme('light', '#f0eeeb')
const served = (current, next) => ({ ...current, toggle: next })

// A fake `/api/theme/mode`: answers with `responses[mode]`, optionally held
// until `release()` so a test can look at the DOM before the save resolves.
function makeApi(responses = { light: served(LIGHT, DARK), dark: served(DARK, LIGHT) }, { hold = false, fail = false } = {}) {
  const calls = []
  let release
  const gate = hold ? new Promise((resolve) => { release = resolve }) : Promise.resolve()
  return {
    calls,
    release: () => release(),
    theme: {
      setMode: async (mode) => {
        calls.push(mode)
        await gate
        if (fail) return { ok: false, status: 500, json: async () => ({}) }
        return { ok: true, status: 200, json: async () => responses[mode] }
      },
    },
  }
}

const styleText = () => dom.styleNodes.get('mobius-theme')?.textContent

let dom
let themeService

// applyThemeToDom (called inside toggleTheme) writes the structured theme.
// Stub localStorage so those calls are testable.
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
  const url = new URL('../themeService.js', import.meta.url).href
    + `?t=${Math.random()}`
  themeService = await import(url)
})

test('a warm cache paints the server preview before the save resolves', async () => {
  const qc = makeQueryClient(served(DARK, LIGHT))
  const api = makeApi(undefined, { hold: true })
  const pending = themeService.toggleTheme(qc, 'dark', api)
  await new Promise((resolve) => setImmediate(resolve))
  assert.deepEqual(api.calls, ['light'], 'the save is in flight')
  assert.equal(styleText(), LIGHT.css, 'the preview is painted on the tap')
  assert.equal(dom.meta.content, LIGHT.bg)
  assert.equal(dom.documentElement.getAttribute('data-theme'), 'light')
  api.release()
  const result = await pending
  assert.deepEqual(result, { newMode: 'light', newCss: LIGHT.css, newBg: LIGHT.bg })
})

test('the saved theme is painted when it differs from the preview', async () => {
  // An agent edited theme.css after the shell loaded it: the server rewrites
  // what is on disk, so its answer wins over the preview.
  const edited = theme('light', '#fafafa', ' .x { gap: 1px; }')
  const qc = makeQueryClient(served(DARK, LIGHT))
  const result = await themeService.toggleTheme(qc, 'dark', makeApi({ light: served(edited, DARK) }))
  assert.equal(styleText(), edited.css)
  assert.equal(result.newBg, '#fafafa')
  assert.deepEqual(qc.getQueryData(), served(edited, DARK), 'the cache holds the next preview too')
})

test('a cold cache waits for the server, then paints its answer', async () => {
  const qc = makeQueryClient()
  const api = makeApi(undefined, { hold: true })
  const pending = themeService.toggleTheme(qc, 'dark', api)
  await new Promise((resolve) => setImmediate(resolve))
  assert.equal(styleText(), undefined, 'nothing to paint without a preview')
  api.release()
  await pending
  assert.equal(styleText(), LIGHT.css)
  assert.equal(dom.meta.content, LIGHT.bg)
})

test('a preview for another starting mode is not painted', async () => {
  // The cache says light while the screen shows dark: its preview is dark.
  const qc = makeQueryClient(served(LIGHT, DARK))
  const api = makeApi(undefined, { hold: true })
  const pending = themeService.toggleTheme(qc, 'dark', api)
  await new Promise((resolve) => setImmediate(resolve))
  assert.equal(styleText(), undefined)
  api.release()
  await pending
  assert.deepEqual(api.calls, ['light'])
  assert.equal(styleText(), LIGHT.css)
})

// The shell's service worker answers /api/theme stale-while-revalidate: a
// fetch gets the body the worker held when it started.
function makeServiceWorker(body) {
  const sw = { body }
  globalThis.self = { location: { origin: 'http://shell.test' } }
  globalThis.caches = {
    open: async () => ({ put: async (_url, res) => { sw.body = await res.json() } }),
  }
  return sw
}

for (const [label, start] of [['no preview', DARK], ['a preview', served(DARK, LIGHT)]]) {
  test(`a refetch the save's announcement starts cannot repaint over the answer (${label})`, async (t) => {
    t.after(() => { delete globalThis.caches; delete globalThis.self })
    const sw = makeServiceWorker(start)
    const qc = new QueryClient()
    qc.setQueryData(['theme'], start)
    let refetch
    const api = {
      theme: {
        setMode: async () => {
          // The server publishes theme_updated before it answers; the shell's
          // reaction refetches and the worker serves what it holds now.
          const held = sw.body
          refetch = qc.fetchQuery({
            queryKey: ['theme'],
            queryFn: () => new Promise((resolve) => setTimeout(() => resolve(held), 5)),
          }).catch(() => {})
          return { ok: true, status: 200, json: async () => served(LIGHT, DARK) }
        },
      },
    }
    await themeService.toggleTheme(qc, 'dark', api)
    await refetch
    await new Promise((resolve) => setTimeout(resolve, 10))
    assert.deepEqual(qc.getQueryData(['theme']), served(LIGHT, DARK),
      'the cache keeps the saved theme and its next preview')
    assert.deepEqual(sw.body, served(LIGHT, DARK), 'the next refetch is served the saved theme')
  })
}

test('both theme query keys are seeded and then marked stale without a refetch', async () => {
  const qc = makeQueryClient(served(DARK, LIGHT))
  await themeService.toggleTheme(qc, 'dark', makeApi())
  const seeded = qc.setData.map(([k, v]) => [JSON.stringify(k), v])
  assert.deepEqual(seeded.at(-2), ['["theme"]', served(LIGHT, DARK)])
  assert.deepEqual(seeded.at(-1), ['["theme-mode"]', 'light'])
  assert.deepEqual(
    qc.invalidated.map((o) => [JSON.stringify(o.queryKey), o.refetchType]),
    [['["theme"]', 'none'], ['["theme-mode"]', 'none']],
  )
  assert.ok(qc.events.indexOf('setQueryData') < qc.events.indexOf('invalidate'),
    'the cache holds the new theme before anything is marked stale')
})

test('the first seed is the preview, which is what AppCanvas posts to live frames', async () => {
  const qc = makeQueryClient(served(DARK, LIGHT))
  const api = makeApi(undefined, { hold: true })
  const pending = themeService.toggleTheme(qc, 'dark', api)
  await new Promise((resolve) => setImmediate(resolve))
  const [key, value] = qc.setData[0]
  assert.equal(JSON.stringify(key), '["theme"]')
  assert.equal(value, LIGHT)
  api.release()
  await pending
})

test('a failed save restores the previous paint and cache, and throws', async () => {
  const previous = served(DARK, LIGHT)
  themeService.applyThemeToDom(DARK.css, DARK.bg, DARK.mode)
  const qc = makeQueryClient(previous)
  await assert.rejects(themeService.toggleTheme(qc, 'dark', makeApi(undefined, { fail: true })), /500/)
  assert.equal(styleText(), DARK.css)
  assert.equal(dom.meta.content, DARK.bg)
  assert.equal(qc.getQueryData(), previous)
  assert.equal(qc.invalidated.length, 0, 'nothing is marked stale after a failed save')
})

test('a refused save carries the server reason for the owner', async () => {
  const reason = 'theme.css has an unclosed string, comment or url(), so the light/dark switch cannot rewrite it.'
  const api = { theme: { setMode: async () => ({ ok: false, status: 409, json: async () => ({ detail: reason }) }) } }
  await assert.rejects(themeService.toggleTheme(makeQueryClient(), 'dark', api),
    (err) => err.detail === reason)
})

test('the Settings toggle never uses a View Transition or the per-element fallback', async () => {
  themeService.applyThemeToDom(DARK.css, DARK.bg, DARK.mode)
  let viewTransitionCalled = false
  dom.document.startViewTransition = () => {
    viewTransitionCalled = true
    throw new Error('Settings theme toggle must use the atomic paint path')
  }
  await themeService.toggleTheme(makeQueryClient(served(DARK, LIGHT)), 'dark', makeApi())
  assert.equal(viewTransitionCalled, false)
  assert.equal(dom.documentElement.classList.contains('theme-transitioning'), false)
  assert.equal(dom.meta.content, LIGHT.bg)
})

test('the toggle leaves no inline --bg and records the new mode for the next boot', async () => {
  dom.documentElement.style.setProperty('--bg', '#deadbe')  // stale splash value
  await themeService.toggleTheme(makeQueryClient(served(DARK, LIGHT)), 'dark', makeApi())
  assert.equal(dom.documentElement.style.getPropertyValue('--bg'), undefined)
  assert.deepEqual(JSON.parse(localStorage.getItem('mobius-theme')), { bg: LIGHT.bg, mode: 'light' })
})

// SettingsView derives the direction from getEffectiveTheme().mode, the
// <html data-theme> the last paint set, because its own state lags the paint.
test('repeated toggles driven by the painted mode strictly alternate', async () => {
  themeService.applyThemeToDom(DARK.css, DARK.bg, DARK.mode)
  const qc = makeQueryClient(served(DARK, LIGHT))
  const api = makeApi()
  const seen = []
  for (let i = 0; i < 4; i++) {
    await themeService.toggleTheme(qc, themeService.getEffectiveTheme().mode, api)
    seen.push(themeService.getEffectiveTheme().mode)
  }
  assert.deepEqual(seen, ['light', 'dark', 'light', 'dark'])
  assert.deepEqual(api.calls, ['light', 'dark', 'light', 'dark'])
})
