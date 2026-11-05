/** Browser-computed contract for how the shell and app frame apply a theme:
 * the generated defaults sit in the weakest cascade layer, a font @import
 * stays whole, the stylesheet (not a guessed hint) owns --bg, and the recorded
 * background follows the mode being switched to. Unit tests drive these
 * appliers against DOM stubs and a limited cascade model; only a real browser
 * proves layer precedence and computed values. Fixture-only: it serves the
 * real applier sources to a synthetic origin and needs no login, backend or
 * owner state, hence the dependency-free unauthenticated project. */
import { test, expect } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { dirname, join, normalize } from 'node:path'
import { fileURLToPath } from 'node:url'

import { withThemeDefaults, themeDefaultsForCss } from '../frontend/src/theme.js'

const FRONTEND = join(dirname(fileURLToPath(import.meta.url)), '..', 'frontend')
const SRC = join(FRONTEND, 'src')
const FRAME_HTML = readFileSync(join(FRONTEND, 'public', 'app-frame.html'), 'utf8')
const PREPAINT_SRC = (await import('../frontend/src/lib/applyTheme.js')).PREPAINT_SRC
const ORIGIN = 'http://theme-fixture.test'
const CORE_VARS = Object.keys(themeDefaultsForCss(''))
const FONT_URL = 'https://fonts.googleapis.com/css2?family=Inter:wght@400;600&display=swap'

/** A top-level `function NAME(...) { ... }` from the frame's inline script. */
function frameFunction(name) {
  const m = FRAME_HTML.match(new RegExp(`\\n *function ${name}\\(`))
  if (!m) throw new Error(`function ${name} not found in app-frame.html`)
  let depth = 0
  for (let i = FRAME_HTML.indexOf('{', m.index); i < FRAME_HTML.length; i++) {
    if (FRAME_HTML[i] === '{') depth++
    else if (FRAME_HTML[i] === '}' && --depth === 0) return FRAME_HTML.slice(m.index, i + 1)
  }
  throw new Error(`function ${name} is unbalanced`)
}

// The frame's static fallback palette: the <style> holding its defaults layer.
const FRAME_STATIC_STYLE = [...FRAME_HTML.matchAll(/<style>([\s\S]*?)<\/style>/g)]
  .map(m => m[1]).find(css => css.includes('@layer mobius-defaults'))

const SHELL_PAGE = `<!doctype html><html><head>
<style>@layer mobius-defaults;</style>
<meta name="theme-color" content="#123456"><meta name="color-scheme" content="dark light">
<script type="module">
  import * as applier from '/src/lib/applyTheme.js'
  window.__applyShellTheme = (theme) => applier.applyTheme(theme, { doc: document, store: localStorage })
  window.__ready = true
</script></head><body></body></html>`

const FRAME_PAGE = `<!doctype html><html><head><style>${FRAME_STATIC_STYLE}</style></head><body><script>
var HEX = /^#[0-9a-fA-F]{3,8}$/;
${frameFunction('inferMode')}
${frameFunction('applyTheme')}
window.__applyFrameTheme = applyTheme;
window.__ready = true;
</script></body></html>`

function prepaintPage(slot) {
  const json = JSON.stringify(slot).replace(/</g, '\\u003c')
  return `<!doctype html><html><head>
<script type="application/json" id="__mobius-theme__">${json}</script>
<script>${PREPAINT_SRC}</script></head><body></body></html>`
}

/** Serve `html` at the fixture origin plus the shell sources; block the rest (fonts included). */
async function open(page, html) {
  await page.route('**/*', (route) => {
    const url = new URL(route.request().url())
    if (url.origin !== ORIGIN) return route.abort()
    if (url.pathname === '/') return route.fulfill({ contentType: 'text/html', body: html })
    const file = normalize(join(SRC, url.pathname.replace(/^\/src\//, '')))
    if (url.pathname.startsWith('/src/') && file.startsWith(SRC)) {
      return route.fulfill({ contentType: 'text/javascript', body: readFileSync(file, 'utf8') })
    }
    return route.abort()
  })
  await page.goto(`${ORIGIN}/`)
  await page.waitForFunction(() => window.__ready === true || document.readyState === 'complete')
}

const applyIn = {
  shell: (page, css, bg, mode) => page.evaluate(t => window.__applyShellTheme(t), { css, bg, mode }),
  frame: (page, css, bg) => page.evaluate(([c, b]) => window.__applyFrameTheme(c, b), [css, bg]),
}
const pages = { shell: SHELL_PAGE, frame: FRAME_PAGE }

function rootState(page, names) {
  return page.evaluate((vars) => {
    const root = document.documentElement
    const style = getComputedStyle(root)
    return {
      vars: Object.fromEntries(vars.map(n => [n, style.getPropertyValue(n).trim()])),
      inlineBg: root.style.getPropertyValue('--bg'),
      bodyBg: getComputedStyle(document.body).backgroundColor,
      themeColor: document.querySelector('meta[name="theme-color"]')?.getAttribute('content') ?? null,
      persisted: (() => { try { return JSON.parse(localStorage.getItem('mobius-theme')) } catch { return null } })(),
      links: [...document.querySelectorAll('link[data-theme-font]')].map(l => l.href),
      styleText: document.getElementById('mobius-theme')?.textContent ?? '',
    }
  }, names)
}

function expectEveryCoreVarResolves(state) {
  for (const name of CORE_VARS) expect(state.vars[name], `${name} resolves`).not.toBe('')
}

for (const surface of ['shell', 'frame']) {
  test.describe(`${surface} theme application`, () => {
    test('an owner root rule in any cascade layer beats the generated defaults', async ({ page }) => {
      await open(page, pages[surface])
      const owner = ':root { --accent: #00ffff; --text: #112233; }'
      for (const css of [
        `@layer owner { ${owner} }`,
        `@layer { ${owner} }`,
        `@layer base, owner;\n@layer owner { ${owner} }\n@layer base { :root { --accent: #ff0000; } }`,
      ]) {
        await applyIn[surface](page, withThemeDefaults(css), '#0d0d0d', 'dark')
        const state = await rootState(page, CORE_VARS)
        expect(state.vars['--accent'], css).toBe('#00ffff')
        expect(state.vars['--text'], css).toBe('#112233')
        expectEveryCoreVarResolves(state)
      }
    })

    test('a multi-weight font import stays one whole link and the defaults still apply', async ({ page }) => {
      await open(page, pages[surface])
      for (const imp of [`@import url("${FONT_URL}");`, `@import url(${FONT_URL});`]) {
        await applyIn[surface](page, withThemeDefaults(`${imp}\n:root { --bg: #112233; }`), '#112233', 'dark')
        const state = await rootState(page, CORE_VARS)
        expect(state.links).toEqual([FONT_URL])
        expect(state.styleText).not.toContain('@import')
        expect(state.vars['--bg']).toBe('#112233')
        expectEveryCoreVarResolves(state)
      }
    })

    test('an escaped ) in a bare import URL keeps the import whole and the owner root', async ({ page }) => {
      await open(page, pages[surface])
      const css = withThemeDefaults('@import url(https://example.test/a\\);b.css);\n:root { --accent: #00ffff; }')
      await applyIn[surface](page, css, '#0d0d0d', 'dark')
      const state = await rootState(page, CORE_VARS)
      expect(state.links).toEqual(['https://example.test/a);b.css'])
      expect(state.styleText).not.toContain('b.css')
      expect(state.vars['--accent']).toBe('#00ffff')
      expectEveryCoreVarResolves(state)
    })

    test('an html-rule background wins over a dark hint and leaves no inline --bg', async ({ page }) => {
      await open(page, pages[surface])
      const css = withThemeDefaults('html { --bg: #ffffff; --text: #111111; }')
      await applyIn[surface](page, css, '#0d0d0d', 'light')
      const state = await rootState(page, CORE_VARS)
      expect(state.inlineBg).toBe('')
      expect(state.vars['--bg']).toBe('#ffffff')
      expect(state.vars['--text']).toBe('#111111')
      expect(state.bodyBg).toBe('rgb(255, 255, 255)')
      if (surface === 'shell') expect(state.themeColor).toBe('#ffffff')
    })
  })
}

test('the shell records the background of the mode it switches to', async ({ page }) => {
  // Mode-keyed themes resolve a different --bg once data-theme flips; the
  // theme-color meta and next-boot hint must follow the new mode.
  await open(page, SHELL_PAGE)
  const css = withThemeDefaults(
    ':root[data-theme="dark"] { --bg: #000000; }\n:root[data-theme="light"] { --bg: #ffffff; }')
  await applyIn.shell(page, css, '#000000', 'dark')
  expect((await rootState(page, ['--bg'])).themeColor).toBe('#000000')
  await applyIn.shell(page, css, '#000000', 'light')
  const state = await rootState(page, ['--bg'])
  expect(state.vars['--bg']).toBe('#ffffff')
  expect(state.themeColor).toBe('#ffffff')
  expect(state.persisted).toEqual({ bg: '#ffffff', mode: 'light' })
})

test('the pre-paint script lets the served stylesheet own the background', async ({ page }) => {
  const css = withThemeDefaults('html { --bg: #ffffff; --text: #111111; }')
  await open(page, prepaintPage({ css, bg: '#0d0d0d', mode: 'light' }))
  const state = await rootState(page, CORE_VARS)
  expect(state.inlineBg).toBe('')
  expect(state.vars['--bg']).toBe('#ffffff')
  expectEveryCoreVarResolves(state)
})
