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
    // Keep the INLINE --bg on <html> in lockstep with the theme we
    // just painted. index.html's splash script sets this inline var
    // from localStorage['mobius-theme-bg'] before first paint; an
    // inline style on documentElement beats `:root{}` in the cascade,
    // so a stale value here pins the wrong background even after the
    // <style id="mobius-theme"> block updates. Re-set it (not remove)
    // so the no-flash-on-load benefit survives, and write the same
    // key back so the NEXT cold-boot splash reads the current bg.
    document.documentElement.style.setProperty('--bg', bg)
    try { localStorage.setItem('mobius-theme-bg', bg) } catch {}
  }

  // Tell the apps-sdk-ui design system which mode we're in. SDK
  // tokens like --color-surface-elevated, --color-text, --color-bg
  // are scoped under `:where([data-theme="dark"|"light"])` blocks;
  // without this attribute the SDK defaults to LIGHT tokens, which
  // make the SDK Menu render a white panel on top of our dark
  // shell. Mode is inferred from --bg luminance — light backgrounds
  // mean light mode regardless of how the user got there.
  const mode = _inferThemeMode(cssBody, bg)
  if (mode) {
    document.documentElement.setAttribute('data-theme', mode)
    // iOS status bar — without this it stays "black" regardless of
    // the active theme. `default` paints a white bar with dark
    // glyphs (right for light mode), `black` is opaque dark (right
    // for dark mode). Only matters when installed as a PWA.
    const sb = document.querySelector(
      'meta[name="apple-mobile-web-app-status-bar-style"]'
    )
    if (sb) sb.setAttribute('content', mode === 'light' ? 'default' : 'black')
  }
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
 * Read the theme the shell has ALREADY APPLIED to its own DOM —
 * what the user currently sees — as `{ css, bg, mode }`.
 *
 * This is the offline-safe source of truth for propagating the
 * theme into mini-app iframes. `applyThemeToDom` injects the
 * effective CSS into `<style id="mobius-theme">`, sets
 * `document.body.style.background`, and `data-theme` on
 * `<html>`. Those are present whenever a theme has been applied —
 * including cold offline reopens — unlike the `/api/theme` React
 * Query result (`theme?.css`), which is `undefined` until the
 * network query resolves. AppCanvas posts THIS into the frame so a
 * cached frame (whose server-injected theme may be stale/dark from
 * a pre-toggle fetch) repaints to the current theme on mount,
 * online or offline, with no refetch.
 *
 * Returns `null` when no theme block has been injected yet (very
 * early boot, before `useTheme`'s first applyThemeToDom) — callers
 * fall back to whatever they have.
 */
export function getEffectiveTheme() {
  if (typeof document === 'undefined') return null
  const el = document.getElementById(STYLE_ID)
  const css = el?.textContent || ''
  if (!css) return null
  // body.style.background is the inline value applyThemeToDom set
  // from the theme's --bg; only trust it if it's a clean hex (the
  // same constraint applyThemeToDom + the frame's applyTheme use).
  const rawBg = (document.body?.style?.background || '').trim()
  const bg = HEX_RE.test(rawBg) ? rawBg : undefined
  // Prefer the attribute the shell already set (authoritative);
  // fall back to inferring from the CSS body if absent.
  const mode = document.documentElement.getAttribute('data-theme')
    || _inferThemeMode(css, bg)
  return { css, bg, mode }
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

// The shell's service worker serves `/api/theme` StaleWhileRevalidate from
// the `mobius-shell-data` cache (see sw.js). Right after a toggle,
// `themeQueries.invalidate` triggers a refetch whose SWR response is the
// PRE-toggle (stale) theme — and `useTheme`'s apply effect then repaints
// that stale theme OVER the toggle's correct optimistic paint, snapping
// the UI back to the old mode (the "stuck on dark / reverts" symptom).
// We just persisted the authoritative new theme, so overwrite the cached
// `/api/theme` body with it: the SWR serve is now correct and the re-apply
// is a no-op instead of a regression. Best-effort — no SW / no Cache
// Storage (tests, SSR, private mode) just skips this; the optimistic paint
// + the eventual network revalidation still converge.
const SHELL_DATA_CACHE = 'mobius-shell-data'

async function _refreshThemeSwCache(css, bg) {
  if (typeof caches === 'undefined' || typeof Response === 'undefined') return
  try {
    const cache = await caches.open(SHELL_DATA_CACHE)
    const url = new URL('/api/theme', self?.location?.origin || window.location.origin).href
    const body = JSON.stringify({ css, bg })
    await cache.put(url, new Response(body, {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }))
  } catch {
    // Cache write failed (quota, opaque origin, no SW). Non-fatal: the
    // optimistic DOM paint already reflects the new theme.
  }
}

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
  // Spread the full base palette FIRST so any token missing from the
  // persisted file (e.g. --accent/--danger/--green after a prior
  // toggle stripped them) is restored with the new mode's value;
  // meta.colors then preserves the user's customisations (their
  // purple accent survives), and swapped applies the structural
  // mode-swap last. Without the base spread, a toggle re-persisted
  // whatever was already missing — degrading theme.css on every swap
  // until light mode rendered with no accents at all.
  const colors = { ...base, ...meta.colors, ...swapped }
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
    // Make the post-toggle refetch resolve to the NEW theme, not the stale
    // SWR-cached one — otherwise useTheme's apply effect would repaint the
    // old theme over this toggle's correct paint (the revert/stuck bug).
    // setQueryData updates the in-memory cache immediately; the SW cache
    // refresh keeps the SWR network revalidation from re-introducing stale
    // data on the next read.
    queryClient.setQueryData(themeQueries.keys.all, { css: newCss, bg: newBg })
    await _refreshThemeSwCache(newCss, newBg)
    themeQueries.invalidate(queryClient)
    themeQueries.mode.invalidate(queryClient)
  } catch (err) {
    applyThemeToDom(currentCss, oldBg)
    throw err
  }

  return { newMode, newCss, newBg }
}
