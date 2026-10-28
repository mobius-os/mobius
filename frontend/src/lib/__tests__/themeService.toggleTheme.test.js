/**
 * Unit tests for themeService.toggleTheme (Commit 3a).
 *
 * These are the SHIP-BLOCKER tests from the v2 design:
 *   - Fix 1: toggleTheme MUST seed BOTH theme query keys and mark
 *            them stale, otherwise AppCanvas iframe propagation or
 *            SettingsView's switch state silently drifts.
 *   - Fix 2: toggleTheme MUST extract `newBg` from the BUILT CSS,
 *            not from the parsed-input meta.
 *
 * Plus a unit assertion that the cache seed IS what triggers
 * AppCanvas's useEffect path. AppCanvas reads
 * `useQuery({ queryKey: themeQueryKey })`; its useEffect on
 * [theme?.css, theme?.bg] postMessages the iframe. We simulate that
 * effect with a mock queryClient that captures setQueryData + a
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

// Mock queryClient — records every invalidateQueries call so we can
// assert BOTH theme keys were invalidated.
function makeQueryClient(seedCache) {
  const invalidated = []
  const setData = []  // [queryKey, value] pairs from setQueryData
  // Mirror the real cache: getQueryData reads the seeded / last-set value.
  // Default cold (undefined) so existing tests still exercise toggleTheme's
  // getThemeCss fallback; a warm seed exercises the synchronous-swap path.
  let cache = seedCache
  return {
    invalidated,
    setData,
    getQueryData: () => cache,
    // toggleTheme seeds the theme query cache with the new {css,bg} so the
    // post-invalidate refetch / useTheme re-apply can't repaint a stale
    // SWR value over the toggle's correct paint.
    setQueryData: (queryKey, value) => { setData.push([queryKey, value]); cache = value; return value },
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
  --surface-2: #1c2028;
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
      getThemeCss: () => {
        calls.push(['getThemeCss'])
        return Promise.resolve({
          ok: true,
          text: () => Promise.resolve(currentCss),
        })
      },
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
  assert.equal(dom.meta.content, '#f0eeeb')
})

test('I4 — warm cache: toggleTheme paints WITHOUT awaiting getThemeCss (instant recolor)', async () => {
  // Owner-reported lag: applyThemeToDom was gated behind `await getThemeCss()`,
  // so the toggle (and page) recolored a frame after the knob slid. With the
  // theme query cache warm (normal once useTheme has run), the swap source is
  // read synchronously and getThemeCss is never hit — the repaint lands on the
  // click tick.
  const qc = makeQueryClient({ css: DARK_CSS, bg: '#0d0f14' })
  const api = makeApi(DARK_CSS)
  const result = await themeService.toggleTheme(qc, 'dark', api)
  assert.ok(!api.calls.some(c => c[0] === 'getThemeCss'),
    `getThemeCss must NOT be fetched when the theme cache is warm (it gates the optimistic paint). Got: ${api.calls.map(c=>c[0]).join(', ')}`)
  assert.equal(result.newMode, 'light')
  assert.equal(dom.meta.content, '#f0eeeb')
  assert.ok(api.calls.some(c => c[0] === 'putThemeCss'), 'still persists the new css')
})

test('I4 — cold cache: toggleTheme falls back to fetching the persisted css', async () => {
  const qc = makeQueryClient()  // cold (no seed)
  const api = makeApi(DARK_CSS)
  const result = await themeService.toggleTheme(qc, 'dark', api)
  assert.ok(api.calls.some(c => c[0] === 'getThemeCss'),
    'cold cache MUST fall back to fetching the persisted css as the swap source')
  assert.equal(result.newMode, 'light')
})

test('toggleTheme persists css + mode before invalidating', async () => {
  // Order matters: if invalidation fires before persist resolves,
  // a refetch could read the OLD css from the server and apply
  // it back, undoing the toggle.
  const events = []
  const qc = {
    invalidated: [],
    getQueryData: () => undefined,
    setQueryData: (queryKey, value) => {
      events.push(['setQueryData', JSON.stringify(queryKey)])
      return value
    },
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

test('REGRESSION — toggleTheme preserves selectors containing -- in extra CSS', async () => {
  // The Settings server spacing rule uses `.settings__section--compact`.
  // parseThemeMeta used to scan the whole stylesheet for custom properties,
  // so it treated the selector's `--compact:last-child...` as a bogus CSS
  // variable. The next toggle rebuilt theme.css with `--compact: ...` inside
  // :root and removed the actual rule.
  const cssWithSettingsRule = `${DARK_CSS}
  /* settings server spacing: keep Restart and status actions visually separate. */
  .settings__section--server,
  .settings .settings__content > .settings__section.settings__section--compact:last-child .settings__row:last-child {
    gap: 14px;
  }
  `
  const qc = makeQueryClient()
  const api = makeApi(cssWithSettingsRule)
  await themeService.toggleTheme(qc, 'dark', api)
  const putCall = api.calls.find(c => c[0] === 'putThemeCss')
  assert.ok(putCall, 'putThemeCss must have been called')
  const newCss = putCall[1]
  assert.ok(newCss.includes('.settings__section--server'),
    'settings spacing rule must remain outside :root')
  assert.ok(newCss.includes('.settings__section.settings__section--compact:last-child'),
    'selector containing --compact must be preserved as a selector')
  assert.equal(/^\s*--compact\s*:/m.test(newCss), false,
    `selector text must not be converted into a bogus --compact variable:\n${newCss}`)
})

test('REGRESSION — toggling an accent-stripped theme restores the missing tokens', async () => {
  // The prod "light mode completely broken" bug: a prior toggle had
  // re-persisted theme.css with ONLY structural tokens (no --accent /
  // --accent-hover / --accent-dim / --danger / --green), because the
  // old `{ ...meta.colors, ...swapped }` build dropped any token the
  // input lacked. Each subsequent toggle degraded it further. The fix
  // spreads the full base palette first, so a toggle always re-emits a
  // COMPLETE theme. This is the lock-in.
  const STRIPPED_LIGHT = `:root {
  --bg: #f0eeeb;
  --surface: #ffffff;
  --surface-2: #e8e6e2;
  --border: #d4d1cc;
  --border-light: #e2dfdb;
  --text: #1c1b1a;
  --muted: #6b6864;
  --font: 'Inter', system-ui, sans-serif;
  --mono: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 15px;
  color-scheme: light;
}
`
  const qc = makeQueryClient()
  const api = makeApi(STRIPPED_LIGHT)
  await themeService.toggleTheme(qc, 'light', api)
  const putCall = api.calls.find(c => c[0] === 'putThemeCss')
  assert.ok(putCall, 'putThemeCss must have been called')
  const newCss = putCall[1]
  for (const token of ['--accent:', '--accent-hover:', '--accent-dim:', '--danger:', '--green:']) {
    assert.ok(newCss.includes(token),
      `toggling a stripped theme must restore ${token} from the base palette; got:\n${newCss}`)
  }
  // And the structural swap still happened (light -> dark bg).
  assert.ok(newCss.includes('--bg: #0d0d0d'), 'structural --bg must swap to dark')
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

test('iframe-propagation contract — cache seed triggers AppCanvas-style postMessage', async () => {
  // Mandatory smoke: verify that the cache seed is what AppCanvas's
  // useEffect uses to trigger the moebius:frame-theme postMessage.
  // We can't run the real AppCanvas under node:test (no React
  // renderer), but we can mock its observable contract: subscribers
  // to ['theme'] receive the new theme and call postMessage.
  const qc = makeQueryClient()
  const api = makeApi(DARK_CSS)
  const postedMessages = []
  let lastTheme

  // Simulate AppCanvas's useQuery subscriber + useEffect on
  // [theme?.css, theme?.bg].
  const origSetQueryData = qc.setQueryData
  qc.setQueryData = (queryKey, value) => {
    const result = origSetQueryData(queryKey, value)
    if (JSON.stringify(queryKey) === '["theme"]') {
      const changed = !lastTheme
        || lastTheme.css !== value.css
        || lastTheme.bg !== value.bg
      lastTheme = value
      if (changed) {
        // Stand-in for AppCanvas's effect: after the theme query data
        // changes, the effect posts to the iframe with the new css + bg.
        postedMessages.push({
          type: 'moebius:frame-theme',
          themeCss: value.css,
          bg: value.bg,
        })
      }
    }
    return result
  }

  await themeService.toggleTheme(qc, 'dark', api)
  assert.equal(postedMessages.length, 1,
    'AppCanvas-style subscriber must observe the ["theme"] cache seed and postMessage exactly once')
  assert.equal(postedMessages[0].type, 'moebius:frame-theme')
  assert.equal(postedMessages[0].bg, '#f0eeeb')
})

test('theme queries are marked stale without immediate refetch after persist', async () => {
  const qc = makeQueryClient()
  const api = makeApi(DARK_CSS)
  await themeService.toggleTheme(qc, 'dark', api)
  assert.deepEqual(
    qc.invalidated.map(k => JSON.stringify(k)),
    ['["theme"]', '["theme-mode"]'],
    'both theme query keys should be marked stale after persist',
  )
})


// --- BUG 1: toggle direction must come from the VISIBLE (DOM) mode ---
// SettingsView.toggleTheme used to derive direction from its optimistic
// `lightMode` state, which mirrors themeModeQuery.data and LAGS the painted
// theme through the SW. When it lagged, the toggle computed the wrong
// direction and applyThemeToDom got handed the already-current CSS → a
// no-op repaint that left the UI stuck. The fix reads the direction from
// getEffectiveTheme().mode (the <html data-theme> applyThemeToDom last
// painted). These tests lock in that the effective DOM mode is the
// authoritative, always-current source a correct toggle must key off — so
// a lagging `lightMode` can never send the toggle the wrong way.

test('BUG 1 — getEffectiveTheme().mode reflects the PAINTED theme, not a lagging state', async () => {
  // Paint dark, then light, via the real apply path (what the user sees).
  themeService.applyThemeToDom(':root { --bg: #0d0d0d; }', '#0d0d0d')
  assert.equal(themeService.getEffectiveTheme().mode, 'dark')
  themeService.applyThemeToDom(':root { --bg: #f0eeeb; }', '#f0eeeb')
  // The visible mode is now light. A lagging `lightMode=false` (dark)
  // would disagree — but the toggle keys off THIS, which is correct.
  assert.equal(themeService.getEffectiveTheme().mode, 'light',
    'effective mode must follow the painted theme so toggle direction is right')
})

test('BUG 1 — direction derived from effective DOM mode toggles correctly even when lightMode lags', async () => {
  // Simulate SettingsView.toggleTheme's NEW direction logic against a
  // VISIBLE light theme while the optimistic `lightMode` still says dark
  // (the lag that caused the stuck-on-dark bug).
  themeService.applyThemeToDom(':root { --bg: #f0eeeb; }', '#f0eeeb')  // user sees LIGHT
  const laggingLightMode = false  // stale: still thinks we're dark

  // OLD (buggy) logic: newMode = !lightMode -> dark; currentMode -> 'light'
  // by coincidence here, but the general failure is direction off the lag.
  // NEW logic mirrors SettingsView: derive from the effective DOM mode.
  const eff = themeService.getEffectiveTheme()
  const currentMode = eff?.mode === 'light' || eff?.mode === 'dark'
    ? eff.mode
    : (laggingLightMode ? 'light' : 'dark')
  assert.equal(currentMode, 'light',
    'currentMode must be read from the VISIBLE theme (light), ignoring the lagging state')

  // Toggling from the visible light theme must persist DARK and repaint dark.
  const qc = makeQueryClient()
  const LIGHT_CSS = DARK_CSS
    .replace('--bg: #0d0f14', '--bg: #f0eeeb')
    .replace('color-scheme: dark', 'color-scheme: light')
  const api = makeApi(LIGHT_CSS)
  const result = await themeService.toggleTheme(qc, currentMode, api)
  assert.equal(result.newMode, 'dark',
    'toggling away from the visible LIGHT theme must go DARK (not stay/flip wrong)')
  assert.equal(dom.meta.content, '#0d0d0d',
    'the DOM must repaint to the new dark bg (no stuck no-op)')
  assert.equal(themeService.getEffectiveTheme().mode, 'dark',
    'after the toggle the visible mode is dark')
})

test('BUG 1 — repeated toggles driven by effective mode never get stuck', async () => {
  // The owner's exact failure: dark -> light -> dark -> light rapidly.
  // Each toggle re-reads the effective DOM mode, so direction is always
  // right and the paint always changes.
  let css = DARK_CSS  // start dark
  const seen = []
  for (let i = 0; i < 4; i++) {
    const eff = themeService.getEffectiveTheme()
    // First iteration: nothing painted yet, fall back to known dark start.
    const currentMode = eff?.mode === 'light' || eff?.mode === 'dark'
      ? eff.mode
      : 'dark'
    const qc = makeQueryClient()
    const api = makeApi(css)
    const result = await themeService.toggleTheme(qc, currentMode, api)
    css = api.calls.find(c => c[0] === 'putThemeCss')[1]  // new persisted css
    seen.push(themeService.getEffectiveTheme().mode)
  }
  // Strictly alternating, never stuck.
  assert.deepEqual(seen, ['light', 'dark', 'light', 'dark'],
    `repeated toggles must strictly alternate; got ${seen.join(', ')}`)
})

test('BUG 2 — toggleTheme leaves no stale inline --bg over the painted theme', async () => {
  // toggleTheme -> applyThemeToDom removes any inline --bg: the theme
  // stylesheet owns it, and an inline value would outrank the owner's.
  dom.documentElement.style.setProperty('--bg', '#deadbe')  // stale splash value
  const qc = makeQueryClient()
  const api = makeApi(DARK_CSS)  // dark -> light
  await themeService.toggleTheme(qc, 'dark', api)
  assert.equal(dom.documentElement.style.getPropertyValue('--bg'), undefined,
    'no inline --bg may outrank the toggled-to theme stylesheet')
  assert.equal(dom.meta.content, '#f0eeeb')
  // Delegation: applyThemeToDom persists the {bg,mode} key the pre-paint IIFE reads.
  assert.deepEqual(
    JSON.parse(localStorage.getItem('mobius-theme')),
    { bg: '#f0eeeb', mode: 'light' },
    'toggle must persist the new {bg,mode} key so the next cold boot resolves light',
  )
})


// --- REVERT RACE: toggle must not be clobbered by a stale /api/theme re-apply ---
// Reproduced in-browser: after a toggle, an immediate /api/theme refetch could
// receive the StaleWhileRevalidate PRE-toggle theme; useTheme's apply effect
// then repainted that stale theme OVER the toggle's correct paint, snapping
// the UI back (the owner's "stuck/reverts" symptom). The fix seeds the theme
// query cache with the NEW {css,bg} via setQueryData, refreshes the SW cache,
// and marks the queries stale without same-tick refetch churn.

test('REVERT RACE — toggleTheme seeds the theme query cache with the NEW theme', async () => {
  const qc = makeQueryClient()
  const api = makeApi(DARK_CSS)  // dark -> light
  const result = await themeService.toggleTheme(qc, 'dark', api)
  // setQueryData(['theme'], {css,bg}) must have fired with the NEW light theme.
  const themeSet = qc.setData.find(([k]) => JSON.stringify(k) === '["theme"]')
  assert.ok(themeSet, 'toggleTheme must seed the ["theme"] query cache so a stale SWR re-apply cannot clobber it')
  assert.equal(themeSet[1].bg, '#f0eeeb',
    `seeded bg must be the NEW light bg, not the stale dark one. Got ${themeSet[1].bg}`)
  assert.equal(themeSet[1].css, result.newCss,
    'seeded css must equal the freshly-built/applied css')
})

test('NAV REGRESSION — Settings toggleTheme avoids View Transition snapshot navigation', async () => {
  const qc = makeQueryClient()
  const api = makeApi(DARK_CSS)
  let viewTransitionCalled = false
  dom.document.startViewTransition = () => {
    viewTransitionCalled = true
    throw new Error('Settings theme toggle must use the atomic paint path')
  }

  const result = await themeService.toggleTheme(qc, 'dark', api)

  assert.equal(viewTransitionCalled, false,
    'theme toggles from Settings should not invoke document.startViewTransition')
  assert.equal(dom.document.documentElement.classList.contains('theme-transitioning'), false,
    'theme toggles from Settings should not animate descendant colors independently')
  assert.equal(result.newMode, 'light')
  assert.equal(dom.meta.content, '#f0eeeb')
})

test('REVERT RACE — query cache is seeded BEFORE stale marking', async () => {
  // setQueryData must land before invalidateQueries: invalidation marks the
  // query stale for later refresh, but the cache must already hold the new
  // theme so all subscribers see a coherent target immediately.
  const events = []
  const qc = {
    setData: [],
    getQueryData: () => undefined,
    setQueryData: (queryKey, value) => { events.push('setQueryData'); qc.setData.push([queryKey, value]); return value },
    invalidated: [],
    invalidateQueries: (opts) => { events.push('invalidate'); qc.invalidated.push(opts.queryKey); return Promise.resolve() },
  }
  const api = makeApi(DARK_CSS)
  await themeService.toggleTheme(qc, 'dark', api)
  const setIdx = events.indexOf('setQueryData')
  const invIdx = events.indexOf('invalidate')
  assert.ok(setIdx >= 0 && invIdx >= 0, 'both setQueryData and invalidate must fire')
  assert.ok(setIdx < invIdx,
    `setQueryData must precede invalidate so the refetch/re-apply sees the new theme; got ${events.join(' -> ')}`)
})

test('toggleTheme derives --accent-fg from the accent it preserved', async () => {
  // The server injects --accent-fg into a TRAILING :root that parseThemeMeta
  // does not read, so it is absent from meta.colors here. A fixed palette
  // value would be spread back over the derivation; persisting even the
  // derived value into the first :root would stop _ensure_core_vars from ever
  // re-deriving it after a later --accent edit. So the toggle PAINTS the
  // derived foreground and leaves theme.css without it.
  const whiteAccent = DARK_CSS.replace('--accent: #ff00ff', '--accent: #ffffff')
  const qc = makeQueryClient()
  const api = makeApi(whiteAccent)
  const { newCss: painted } = await themeService.toggleTheme(qc, 'dark', api)
  const newCss = api.calls.find(c => c[0] === 'putThemeCss')[1]
  assert.ok(newCss.includes('--accent: #ffffff'), 'accent survives the toggle')
  assert.ok(painted.includes('--accent-fg: #000000'),
    'a white accent must not keep a white foreground through a toggle')
  assert.equal(newCss.includes('--accent-fg'), false,
    'the derived foreground is painted, never frozen into theme.css')
})

test('toggleTheme keeps a foreground the theme declared for itself', async () => {
  const declared = DARK_CSS.replace(
    '--accent: #ff00ff',
    '--accent: #ffffff; --accent-fg: #123456',
  )
  const qc = makeQueryClient()
  const api = makeApi(declared)
  await themeService.toggleTheme(qc, 'dark', api)
  const newCss = api.calls.find(c => c[0] === 'putThemeCss')[1]
  assert.ok(newCss.includes('--accent-fg: #123456'),
    'an explicitly declared foreground is never overwritten by derivation')
})

// ---------------------------------------------------------------------------
// Mode-owned defaults vs genuine customisations across a light/dark toggle.
// A shipped default of either mode belongs to the mode and is swapped; any
// other value is the owner's and survives. Role tokens (--accent-text,
// --accent-fill, ...) are painted for the new mode but never persisted unless
// the theme declared them, so a later --accent edit still re-derives them.
// ---------------------------------------------------------------------------

function persistedCss(api) {
  return api.calls.filter(c => c[0] === 'putThemeCss').at(-1)[1]
}

function rootVar(css, name) {
  const m = css.match(new RegExp(`(?:^|[\\s;{])${name}\\s*:\\s*([^;]+);`))
  return m ? m[1].trim() : undefined
}

async function toggleOnce(css, fromMode) {
  const qc = makeQueryClient({ css })
  const api = makeApi(css)
  const result = await themeService.toggleTheme(qc, fromMode, api)
  return { result, persisted: persistedCss(api) }
}

const { DARK_COLORS, LIGHT_COLORS, DARK_ROLES, LIGHT_ROLES } = await import('../../theme.js')

function shippedCss(colors, mode) {
  const vars = Object.entries(colors).map(([k, v]) => `  ${k}: ${v};`).join('\n')
  return `:root {\n  /* Colors - ${mode} theme */\n${vars}\n  --font: 'Inter', sans-serif;\n  --mono: monospace;\n  font-size: 15px;\n  color-scheme: ${mode};\n}\n`
}

test('ROUND TRIP — dark → light → dark returns every shipped dark default', async () => {
  const first = await toggleOnce(shippedCss(DARK_COLORS, 'dark'), 'dark')
  for (const [k, v] of Object.entries(LIGHT_COLORS)) {
    assert.equal(rootVar(first.persisted, k), v, `light ${k}`)
  }
  const back = await toggleOnce(first.persisted, 'light')
  for (const [k, v] of Object.entries(DARK_COLORS)) {
    assert.equal(rootVar(back.persisted, k), v, `dark ${k} after the round trip`)
  }
})

test('REGRESSION — light status defaults carried into a dark theme are swapped, not kept as custom', async () => {
  // The pre-fix toggle swapped only structural keys, so many dark theme.css
  // files carry light --danger/--green/--accent-dim. Those are shipped values,
  // not customisations: the next toggle must give the target mode's own.
  const carried = shippedCss({
    ...DARK_COLORS,
    '--danger': LIGHT_COLORS['--danger'],
    '--green': LIGHT_COLORS['--green'],
    '--accent-dim': LIGHT_COLORS['--accent-dim'],
  }, 'dark')
  const toLight = await toggleOnce(carried, 'dark')
  assert.equal(rootVar(toLight.persisted, '--danger'), LIGHT_COLORS['--danger'])
  const toDark = await toggleOnce(toLight.persisted, 'light')
  assert.equal(rootVar(toDark.persisted, '--danger'), DARK_COLORS['--danger'])
  assert.equal(rootVar(toDark.persisted, '--green'), DARK_COLORS['--green'])
  assert.equal(rootVar(toDark.persisted, '--accent-dim'), DARK_COLORS['--accent-dim'])
})

test('ROUND TRIP — genuine custom values survive both directions', async () => {
  const custom = shippedCss({
    ...DARK_COLORS,
    '--danger': '#ff2d55',
    '--accent-text': '#c4b5fd',
    '--brand-glow': 'rgba(1, 2, 3, 0.5)',
  }, 'dark')
  const toLight = await toggleOnce(custom, 'dark')
  const toDark = await toggleOnce(toLight.persisted, 'light')
  for (const css of [toLight.persisted, toDark.persisted]) {
    assert.equal(rootVar(css, '--danger'), '#ff2d55')
    assert.equal(rootVar(css, '--accent-text'), '#c4b5fd')
    assert.equal(rootVar(css, '--brand-glow'), 'rgba(1, 2, 3, 0.5)')
  }
})

test('the toggle paints the new mode\'s roles without persisting them', async () => {
  const { result, persisted } = await toggleOnce(shippedCss(DARK_COLORS, 'dark'), 'dark')
  for (const [k, v] of Object.entries(LIGHT_ROLES)) {
    assert.equal(rootVar(result.newCss, k), v, `painted light ${k}`)
    assert.equal(rootVar(persisted, k), undefined, `${k} must stay derivable, not frozen into theme.css`)
  }
  assert.equal(rootVar(persisted, '--accent-fg'), undefined)
})

test('shipped role values persisted by an older toggle are recognised and swapped', async () => {
  const old = shippedCss({ ...LIGHT_COLORS, ...LIGHT_ROLES, '--accent-fg': '#ffffff' }, 'light')
  const { result, persisted } = await toggleOnce(old, 'light')
  assert.equal(rootVar(persisted, '--accent-text'), undefined)
  assert.equal(rootVar(result.newCss, '--accent-text'), DARK_ROLES['--accent-text'])
})

test('a custom accent re-derives its roles for each mode instead of carrying the old mode\'s', async () => {
  const teal = shippedCss({ ...DARK_COLORS, '--accent': '#0ea5e9' }, 'dark')
  const toLight = await toggleOnce(teal, 'dark')
  const lightText = rootVar(toLight.result.newCss, '--accent-text')
  const toDark = await toggleOnce(toLight.result.newCss, 'light')
  const darkText = rootVar(toDark.result.newCss, '--accent-text')
  assert.ok(lightText && darkText && lightText !== darkText,
    `light ${lightText} and dark ${darkText} accent text must each suit their mode`)
  assert.equal(rootVar(toDark.persisted, '--accent-text'), undefined)
  assert.equal(rootVar(toDark.persisted, '--accent'), '#0ea5e9')
})

test('REGRESSION — a server-injected defaults block is not persisted back as owner CSS', async () => {
  const served = `${shippedCss(DARK_COLORS, 'dark')}
.keep-me { gap: 4px; }
/* Möbius: injected defaults for variables the theme omitted */
:root {
  --accent-fg: #ffffff;
  --accent-text: #957cf8;
}
`
  const { persisted } = await toggleOnce(served, 'dark')
  assert.ok(persisted.includes('.keep-me { gap: 4px; }'), 'owner extras survive')
  assert.equal(persisted.includes('injected defaults'), false,
    `the injected block must be regenerated, not frozen:\n${persisted}`)
})

const { withThemeDefaults, scanRootDeclarations, INJECTED_DEFAULTS_MARKER } = await import('../../theme.js')

test('REGRESSION — an owner role in a later :root follows the generated defaults', async () => {
  const css = `${shippedCss(DARK_COLORS, 'dark')}
.keep { gap: 4px; }
:root { --accent: #00ffff; --accent-text: #abcdef; }
`
  const { result, persisted } = await toggleOnce(css, 'dark')
  assert.ok(persisted.includes(':root { --accent: #00ffff; --accent-text: #abcdef; }'),
    'the later owner root is kept as owner CSS')
  const painted = result.newCss
  // The generated block is first, so the owner's later rule wins the cascade.
  assert.ok(painted.indexOf(INJECTED_DEFAULTS_MARKER) < painted.indexOf('--accent-text: #abcdef'))
  assert.equal(painted.split(INJECTED_DEFAULTS_MARKER).length, 2, 'exactly one generated block')
  // And the defaults derive from the WINNING cyan accent.
  const block = painted.slice(painted.indexOf(INJECTED_DEFAULTS_MARKER), painted.indexOf('}') + 1)
  assert.equal(block.includes(`--accent-fill: ${LIGHT_ROLES['--accent-fill']};`), false)
  assert.equal(scanRootDeclarations(painted)['--accent-text'], '#abcdef')
})

test('the toggle paints exactly what the server serves for what it persists', async () => {
  const shapes = [
    shippedCss(DARK_COLORS, 'dark'),
    `${shippedCss(DARK_COLORS, 'dark')}\n:root { --accent: #0ea5e9; }\n`,
    `${shippedCss(DARK_COLORS, 'dark')}\n@media (prefers-contrast: more) { :root { --danger: #ff0000; } }\n`,
    `${shippedCss({ ...DARK_COLORS, '--green': '#22c55e' }, 'dark')}\n.chat { --accent-fill: #123456; }\n`,
  ]
  for (const css of shapes) {
    const { result, persisted } = await toggleOnce(css, 'dark')
    // withThemeDefaults is pinned byte-for-byte to backend _ensure_core_vars by themeCssCases.json.
    assert.equal(result.newCss, withThemeDefaults(persisted))
    assert.equal(persisted.includes(INJECTED_DEFAULTS_MARKER), false)
  }
})

test('REGRESSION — the toggle reads the root rule lexically: quoted braces and commented declarations', async () => {
  // A `}` inside a string used to end the root block early, so the toggle
  // read no colours and persisted the rest of the rule as broken extras.
  const css = `:root {
  --glyph: "}";
  --sep: 'a;b';
  /* --accent: #ff0000; */
  --bg: #0d0d0d;
  --accent: #00ffff;
  font-size: 16px;
}
.keep { content: "{"; }
`
  const { persisted } = await toggleOnce(css, 'dark')
  assert.equal(rootVar(persisted, '--accent'), '#00ffff', 'the custom accent survives')
  assert.equal(rootVar(persisted, '--bg'), LIGHT_COLORS['--bg'], 'the structural bg swaps')
  assert.ok(persisted.includes('--glyph: "}";'), `the quoted glyph stays a root value:\n${persisted}`)
  assert.ok(persisted.includes("--sep: 'a;b';"))
  assert.ok(persisted.includes('font-size: 16px;'))
  assert.ok(persisted.includes('.keep { content: "{"; }'), 'the following rule is kept whole')
  assert.equal(persisted.includes('#ff0000'), false, 'a commented-out declaration is not a colour')
  assert.equal(persisted.split(':root').length, 2, `exactly one root rule:\n${persisted}`)
})

test('REGRESSION — CSS the lexer cannot follow is kept verbatim rather than edited', async () => {
  const css = `:root { --bg: #0d0d0d; --accent: #00ffff; }\n.broken { content: "open;\n}\n`
  const { persisted } = await toggleOnce(css, 'dark')
  assert.ok(persisted.includes(css.trim()), `owner CSS is preserved byte for byte:\n${persisted}`)
})

test('REGRESSION — a winning !important root declaration survives a toggle with its importance', async () => {
  const css = ':root { --bg: #0d0d0d; --accent: #00ffff !important; --accent: #ff0000; }\n'
  const { persisted } = await toggleOnce(css, 'dark')
  assert.ok(persisted.includes('--accent: #00ffff !important;'), `the important accent is kept:\n${persisted}`)
  assert.equal(persisted.includes('#ff0000'), false, 'the overridden normal declaration is not resurrected')
  assert.equal(scanRootDeclarations(persisted)['--accent'], '#00ffff')
})
