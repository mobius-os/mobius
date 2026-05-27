import { DARK_COLORS, LIGHT_COLORS, parseThemeMeta, buildThemeCss } from '../theme.js'
import { themeQueries } from '../hooks/queries.js'

/**
 * Owns the theme lifecycle: read, transform, apply (DOM + body bg
 * + meta theme-color), persist (storage API), notify (server SSE),
 * invalidate (React Query). Single source of truth for theme
 * mutations triggered by the Settings UI.
 *
 * Why module-not-hook: applyThemeToDom + persistTheme are pure
 * side-effect helpers callable from anywhere — `useTheme` invokes
 * applyThemeToDom from an effect; SettingsView's toggleTheme will
 * orchestrate persist + apply + invalidate. A hook would add
 * render-cycle coupling the helpers don't need.
 *
 * The AGENT-DRIVEN path (agent writes /data/shared/theme.css
 * directly via the storage API) goes through the SSE
 * `theme_updated` event → Shell's loadTheme → themeQueries
 * invalidate → useTheme refetches → applyThemeToDom. That path
 * still works because Shell's loadTheme also invalidates the
 * query.
 *
 * IFRAME PROPAGATION CONTRACT: invalidating the React Query cache
 * is what triggers AppCanvas's useEffect to postMessage
 * `moebius:frame-theme` to live iframes. If you forget to
 * invalidate after a theme mutation, iframes silently stay on the
 * old theme until next mount. Commit 3a wires this in for
 * toggleTheme.
 */

// `bg` from /api/theme is constrained server-side to a hex color
// via the regex in theme.py:get_bg_color. We re-validate here so a
// future server-side change can't slip something dangerous into
// body.style.background or the meta theme-color tag.
const HEX_RE = /^#[0-9a-fA-F]{3,8}$/

const STYLE_ID = 'mobius-theme'
const FONT_LINK_ATTR = 'data-theme-font'

/**
 * Apply a theme CSS string (and optional bg color) to the DOM:
 *   - Strip `@import url(...)` lines from the CSS body and inject
 *     them as `<link>` tags so fonts load reliably (we mirror the
 *     server-side allowlist in theme.py:_is_safe_import_url so a
 *     `javascript:` URL slipped in here can't become a stylesheet
 *     link element).
 *   - Inject the remaining CSS into a single `<style id="mobius-theme">`
 *     re-appended to the end of `<head>` so it wins the cascade
 *     over the server-injected initial theme block.
 *   - Update `document.body.style.background` and the
 *     `<meta name="theme-color">` tag.
 *
 * Lifted verbatim from useTheme.js's inline applyThemeToDom so
 * SettingsView's toggleTheme can call the same code path instead
 * of duplicating the DOM mutations inline.
 */
export function applyThemeToDom(css, bg) {
  document.querySelectorAll(`link[${FONT_LINK_ATTR}]`).forEach(l => l.remove())

  const imports = []
  const cssBody = css.replace(
    /@import\s+url\(\s*['"]([^'"]+)['"]\s*\)\s*;[^\S\n]*\n?/g,
    (_, url) => { imports.push(url); return '' }
  )
  imports.filter(url => /^https?:\/\//i.test(url)).forEach(url => {
    const link = document.createElement('link')
    link.rel = 'stylesheet'
    link.href = url
    link.dataset.themeFont = '1'
    document.head.appendChild(link)
  })

  let el = document.getElementById(STYLE_ID)
  if (!el) {
    el = document.createElement('style')
    el.id = STYLE_ID
  }
  // Always re-append so this style block is the LAST <head> child
  // and wins the cascade over the server-injected initial theme
  // block. appendChild on an already-attached node moves it; cheap.
  document.head.appendChild(el)
  el.textContent = cssBody

  if (bg && HEX_RE.test(bg)) {
    document.body.style.background = bg
    const meta = document.querySelector('meta[name="theme-color"]')
    if (meta) meta.setAttribute('content', bg)
  }

  // Tell the apps-sdk-ui design system which mode we're in. SDK
  // tokens like --color-surface-elevated, --color-text, --color-bg
  // are scoped under `:where([data-theme="dark"|"light"])` blocks;
  // without this attribute the SDK defaults to LIGHT tokens, which
  // make the SDK Menu render a white panel on top of our dark
  // shell. Mode is inferred from --bg luminance — light backgrounds
  // mean light mode regardless of how the user got there.
  const mode = _inferThemeMode(cssBody, bg)
  if (mode) document.documentElement.setAttribute('data-theme', mode)
}

/** Returns 'dark' if the active --bg is dark, 'light' otherwise.
 *  Reads from the bg arg first (cheap) and falls back to parsing
 *  the CSS body. Returns null if neither resolves to a usable hex. */
function _inferThemeMode(cssBody, bg) {
  let hex = (bg && HEX_RE.test(bg)) ? bg : null
  if (!hex) {
    const m = cssBody.match(/--bg:\s*(#[0-9a-fA-F]{3,8})/)
    if (m) hex = m[1]
  }
  if (!hex) return null
  // Quick luminance check: drop the # and average the RGB octets.
  // 128 splits dark/light cleanly enough — exact perceptual lum
  // isn't needed here, just dark-vs-light direction.
  const raw = hex.slice(1)
  const expanded = raw.length === 3
    ? raw.split('').map(c => c + c).join('')
    : raw.slice(0, 6)
  const r = parseInt(expanded.slice(0, 2), 16)
  const g = parseInt(expanded.slice(2, 4), 16)
  const b = parseInt(expanded.slice(4, 6), 16)
  return (r + g + b) / 3 < 128 ? 'dark' : 'light'
}

/**
 * Persist a theme (CSS + mode) to the server's shared storage and
 * notify active broadcasts via the SSE channel so live agents pick
 * up the change.
 *
 * Throws on failure — callers (SettingsView) wrap in try/catch for
 * the optimistic-state rollback. The `api` parameter defaults to
 * the real client; tests pass a mock to verify call sequence.
 *
 * Note: /notify only reaches active chat broadcasts; iframe cache
 * invalidation is the caller's responsibility (see commit 3a's
 * toggleTheme, which invalidates the React Query cache so
 * AppCanvas can postMessage `moebius:frame-theme` to live
 * iframes).
 */
export async function persistTheme(css, mode, api) {
  await Promise.all([
    api.storage.shared.putThemeCss(css),
    api.storage.shared.putThemeMode(mode),
  ])
  api.notify.send({ type: 'theme_updated' }).catch(() => {})
}

// Structural color vars swapped on a light/dark toggle. Accent
// colors and any agent-set custom vars are preserved via the
// existing meta.colors spread so a user's "purple accent" survives
// a theme toggle.
const STRUCTURAL_KEYS = [
  '--bg',
  '--surface',
  '--surface2',
  '--border',
  '--border-light',
  '--text',
  '--muted',
]

/**
 * Toggle between dark and light: fetch the current CSS, swap
 * structural color vars for the opposite mode, apply to the DOM,
 * persist to storage, then invalidate the React Query cache so
 * AppCanvas can postMessage the new theme to live iframes.
 *
 * Returns `{ newMode, newCss, newBg }` so the caller (SettingsView)
 * can update its optimistic UI state without re-deriving them.
 *
 * Throws on persist failure so the caller can run its rollback
 * (flip lightMode back, surface an error).
 *
 * CRITICAL — cache invalidation contract:
 *   Both `themeQueries.invalidate` AND `themeQueries.mode.invalidate`
 *   MUST fire after persistTheme. The first is what AppCanvas.jsx
 *   subscribes to (useQuery({ queryKey: themeQueryKey, ... })) and
 *   its useEffect on [theme?.css, theme?.bg] is what fires the
 *   `moebius:frame-theme` postMessage to live iframes. The second
 *   is what SettingsView reads to seed `lightMode` after navigation
 *   away and back. Forgetting either silently breaks one consumer:
 *   iframes stale-theme until next mount, OR the dark-mode toggle
 *   flips back to the persisted value after a settings re-open.
 *
 * CRITICAL — bg extraction:
 *   `newBg` is extracted from the BUILT CSS via regex, not from
 *   the parsed `meta.colors['--bg']` of the input. Mode swap
 *   replaces --bg, so the old meta value is stale. Passing the
 *   stale value to applyThemeToDom would leave body.background +
 *   meta theme-color pointing at the previous mode's color even
 *   though the <style> block reflects the new mode.
 */
export async function toggleTheme(queryClient, currentMode, api) {
  const newMode = currentMode === 'dark' ? 'light' : 'dark'

  const themeRes = await api.storage.shared.getThemeCss()
  const currentCss = themeRes.ok ? await themeRes.text() : ''
  const meta = parseThemeMeta(currentCss)

  // Pre-toggle bg, captured from the last persisted CSS — the
  // authoritative rollback target if persistTheme below throws.
  const oldBgMatch = currentCss.match(/--bg:\s*(#[0-9a-fA-F]{3,8})/)
  const oldBg = oldBgMatch ? oldBgMatch[1] : meta.colors['--bg']

  // Swap structural colors while preserving agent customisations.
  const base = newMode === 'light' ? LIGHT_COLORS : DARK_COLORS
  const swapped = {}
  for (const k of STRUCTURAL_KEYS) {
    if (base[k]) swapped[k] = base[k]
  }
  const colors = { ...meta.colors, ...swapped }
  const newCss = buildThemeCss(colors, meta, newMode)

  // Extract NEW bg from the built CSS, not from old meta.
  const bgMatch = newCss.match(/--bg:\s*(#[0-9a-fA-F]{3,8})/)
  const newBg = bgMatch ? bgMatch[1] : meta.colors['--bg']

  // Apply optimistically, then persist. On persist failure the
  // catch rolls the DOM back to `currentCss` so the <style> block
  // and server storage stay consistent — otherwise the user sees
  // the new theme until the next refetch resolves the divergence.
  applyThemeToDom(newCss, newBg)
  try {
    await persistTheme(newCss, newMode, api)
    themeQueries.invalidate(queryClient)
    themeQueries.mode.invalidate(queryClient)
  } catch (err) {
    applyThemeToDom(currentCss, oldBg)
    throw err
  }

  return { newMode, newCss, newBg }
}
