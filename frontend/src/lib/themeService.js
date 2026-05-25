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
