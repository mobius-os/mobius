import {
  DARK_COLORS,
  LIGHT_COLORS,
  THEME_ROLE_KEYS,
  parseThemeMeta,
  buildThemeCss,
  isShippedThemeValue,
  scanRootDeclarations,
  themeBg,
  themeRoleVars,
  withThemeDefaults,
} from '../theme.js'
import { themeQueries } from '../hooks/queries.js'
import { applyTheme, cascadeBg, inferMode } from './applyTheme.js'
import { captureLayoutSpace, clientPointToLayout } from './layoutSpace.js'

/**
 * Owns the theme lifecycle: read, transform, apply (DOM + body bg
 * + meta theme-color), persist (storage API), notify (server SSE),
 * seed/invalidate (React Query). Single source of truth for theme
 * mutations triggered by the Settings UI.
 *
 * Why module-not-hook: applyThemeToDom + persistTheme are pure
 * side-effect helpers callable from anywhere — `useTheme` invokes
 * applyThemeToDom from an effect; SettingsView's toggleTheme will
 * orchestrate cache seed + apply + persist. A hook would add
 * render-cycle coupling the helpers don't need.
 *
 * The AGENT-DRIVEN path (agent writes /data/shared/theme.css
 * directly via the storage API) goes through the SSE
 * `theme_updated` event → Shell's loadTheme → themeQueries
 * invalidate → useTheme refetches → applyThemeToDom. That path
 * still works because Shell's loadTheme also invalidates the
 * query. The local settings-toggle path is faster: it seeds the
 * React Query cache before paint, so Shell/AppCanvas/Settings agree
 * before a refetch can serve stale SWR data.
 *
 * IFRAME PROPAGATION CONTRACT: seeding the React Query theme cache
 * is what triggers AppCanvas's useEffect to postMessage
 * `moebius:frame-theme` to live iframes. If you forget to seed
 * after a local theme mutation, iframes silently stay on the old
 * theme until next mount.
 */

// HEX_RE + the applier live in ./applyTheme.js now (the single source
// of truth shared with the app-frame's pre-paint IIFE). `bg` is still
// re-validated against HEX_RE wherever it is read back so a future
// server-side change can't slip a CSS expression into the DOM.
const STYLE_ID = 'mobius-theme'
let themeAppliedOnce = false
let themeTransitionTimer = null
let themeViewTransitionTimer = null
let themeTransitionToken = 0
let nextThemeTransitionOrigin = null
let lastThemeSignature = null

function signatureForTheme(css, bg, mode) {
  const cssText = css || ''
  const cssBg = bg || themeBg(cssText)
  return JSON.stringify([
    cssText,
    bg || '',
    mode || inferMode(cssBg) || '',
  ])
}

function beginThemeTransition() {
  if (typeof document === 'undefined') return
  if (typeof window !== 'undefined'
      && window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches) {
    return
  }
  const root = document.documentElement
  if (!root?.classList?.add || !root.classList.remove) return
  const timerHost = typeof window !== 'undefined' ? window : globalThis
  root.classList.add('theme-transitioning')
  if (themeTransitionTimer) timerHost.clearTimeout(themeTransitionTimer)
  themeTransitionTimer = timerHost.setTimeout(() => {
    root.classList.remove('theme-transitioning')
    themeTransitionTimer = null
  }, 180)
}

function shouldAnimateThemeChange() {
  if (typeof document === 'undefined') return false
  if (!themeAppliedOnce) return false
  if (typeof window !== 'undefined'
      && window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches) {
    return false
  }
  if (document.visibilityState === 'hidden') return false
  return true
}

function defaultThemeTransitionOrigin() {
  if (typeof window === 'undefined') return null
  const root = document.documentElement
  if (!root.offsetWidth || !root.offsetHeight) return null
  return { x: root.offsetWidth / 2, y: root.offsetHeight / 2 }
}

function applyThemeTransitionOrigin(root, origin) {
  const next = origin || defaultThemeTransitionOrigin()
  root.style.setProperty('--theme-transition-x', next ? `${Math.round(next.x)}px` : '50%')
  root.style.setProperty('--theme-transition-y', next ? `${Math.round(next.y)}px` : '50%')
}

function consumeThemeTransitionOrigin() {
  const origin = nextThemeTransitionOrigin
  nextThemeTransitionOrigin = null
  return origin
}

/**
 * Remember where the user started the theme toggle so the view-transition
 * reveal can bloom from the tap/click instead of cross-fading from nowhere.
 * Keyboard/programmatic toggles fall back to the viewport center.
 */
export function setThemeTransitionOriginFromEvent(event) {
  const native = event?.nativeEvent || event
  const point = native?.touches?.[0]
    || native?.changedTouches?.[0]
    || native
  const x = Number(point?.clientX)
  const y = Number(point?.clientY)
  const root = document.documentElement
  const space = captureLayoutSpace(root)
  if (Number.isFinite(x) && Number.isFinite(y)) {
    nextThemeTransitionOrigin = clientPointToLayout({ x, y }, space)
    return
  }

  const target = event?.currentTarget || event?.target
  const rect = target?.getBoundingClientRect?.()
  if (rect) {
    nextThemeTransitionOrigin = clientPointToLayout({
      x: rect.left + rect.width / 2,
      y: rect.top + rect.height / 2,
    }, space)
  }
}

function runThemeTransition(mutateDom, options = {}) {
  // Some controls need an atomic palette swap rather than an animated
  // document transition. In particular, Settings previously eased every
  // descendant independently, which made borders lead surfaces. Keep the
  // transition system for external theme updates, but let the local toggle
  // opt out completely so the whole palette changes in one paint.
  if (options.animate === false) {
    nextThemeTransitionOrigin = null
    mutateDom()
    return
  }
  const preferViewTransition = options.preferViewTransition !== false
  if (!shouldAnimateThemeChange()) {
    nextThemeTransitionOrigin = null
    mutateDom()
    return
  }

  const root = document.documentElement
  const timerHost = typeof window !== 'undefined' ? window : globalThis

  if (preferViewTransition
      && typeof document.startViewTransition === 'function'
      && root?.classList?.add
      && root.classList.remove
      && root.style?.setProperty) {
    const token = ++themeTransitionToken
    let didMutate = false
    const cleanup = () => {
      if (token !== themeTransitionToken) return
      root.classList.remove('theme-view-transitioning')
      if (themeViewTransitionTimer) timerHost.clearTimeout(themeViewTransitionTimer)
      themeViewTransitionTimer = null
    }
    applyThemeTransitionOrigin(root, consumeThemeTransitionOrigin())
    root.classList.add('theme-view-transitioning')
    if (themeViewTransitionTimer) timerHost.clearTimeout(themeViewTransitionTimer)
    themeViewTransitionTimer = timerHost.setTimeout(cleanup, 520)
    try {
      const transition = document.startViewTransition(() => {
        didMutate = true
        mutateDom()
      })
      const finished = transition?.finished || transition?.ready
      if (finished?.then) {
        finished.catch(() => {}).then(cleanup)
      }
      return
    } catch (err) {
      root.classList.remove('theme-view-transitioning')
      if (themeViewTransitionTimer) {
        timerHost.clearTimeout(themeViewTransitionTimer)
        themeViewTransitionTimer = null
      }
      if (didMutate) throw err
      beginThemeTransition()
      mutateDom()
      return
    }
  }

  nextThemeTransitionOrigin = null
  beginThemeTransition()
  mutateDom()
}

/**
 * Apply a theme CSS string (and optional bg color) to the DOM.
 *
 * Thin delegate to the shared library `applyTheme` (src/lib/applyTheme.js),
 * which is the single source of truth for the DOM mutations (strip+link
 * @import fonts, inject <style id="mobius-theme"> last in <head>, mirror bg
 * onto body / meta theme-color / inline --bg, set data-theme + color-scheme +
 * iOS status bar from the mode) AND for persisting the sole {bg,mode} key.
 * The same library code paints the app-frame's pre-paint
 * IIFE, so the shell and the iframe can never drift.
 *
 * Kept as a named export so existing callers (useTheme's apply effect,
 * toggleTheme below, tests) don't have to change their call shape.
 */
export function applyThemeToDom(css, bg, mode, options = {}) {
  const signature = signatureForTheme(css, bg, mode)
  const mutateDom = () => {
    applyTheme({ css, bg, mode })
    lastThemeSignature = signature
    themeAppliedOnce = true
  }
  if (signature !== lastThemeSignature) runThemeTransition(mutateDom, options)
  else mutateDom()
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
  // The background the applied stylesheet actually resolves (it owns --bg
  // once applied; html/body paint var(--bg)), else the css's pre-paint hint.
  // It is only a hint for the frame, which reads its own cascade too.
  const bg = cascadeBg(document) || themeBg(css)
  // Prefer the attribute the shell already set (authoritative).
  const mode = document.documentElement.getAttribute('data-theme')
    || inferMode(bg)
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
 * Note: /notify only reaches active chat broadcasts; iframe theme
 * propagation is the caller's responsibility. toggleTheme seeds the
 * React Query cache so AppCanvas can postMessage
 * `moebius:frame-theme` to live iframes.
 */
export async function persistTheme(css, mode, api) {
  await Promise.all([
    api.storage.shared.putThemeCss(css),
    api.storage.shared.putThemeMode(mode),
  ])
  api.notify.send({ type: 'theme_updated' }).catch(() => {})
}

// Structural color vars always take the new mode's palette on a light/dark
// toggle: a dark surface is never right in light mode, custom or not.
const STRUCTURAL_KEYS = [
  '--bg',
  '--surface',
  '--surface-2',
  '--border',
  '--border-light',
  '--text',
  '--muted',
]

/**
 * The theme.css colours for `newMode`, built from the colours the theme
 * declared in its first `:root` while in `oldMode`. `oldCss` is the whole
 * stylesheet, whose winning root declarations decide what `oldMode` derived.
 *
 * A declared value is MODE-OWNED, and replaced by the new mode's default,
 * when it is a structural key, a shipped default of either mode (the shipped
 * value of one mode carried into the other by an older toggle is not an owner
 * choice), or a role equal to what `oldMode` derives from the theme's own
 * bases. Every other value is the owner's and survives unchanged, so a
 * custom accent or status colour outlives any number of round trips. The
 * one ambiguity: an owner who deliberately chose a shipped value of the other
 * mode gets it swapped; pick any other value to pin it.
 *
 * Role tokens are not written back unless the owner declared them; the
 * generated defaults block (`withThemeDefaults`) paints them, so a later
 * base edit re-derives them rather than meeting a frozen copy.
 */
function colorsForMode(declared, oldCss, oldMode, newMode) {
  const root = scanRootDeclarations(oldCss) || {}
  const derivedForOldMode = themeRoleVars(
    Object.fromEntries(Object.entries(root).filter(([k]) => !THEME_ROLE_KEYS.includes(k))),
    oldMode,
  )
  const modeOwned = (key, value) => STRUCTURAL_KEYS.includes(key)
    || isShippedThemeValue(key, value)
    || (THEME_ROLE_KEYS.includes(key) && derivedForOldMode[key] === value)
  // The full base palette first, so a token a prior toggle stripped is
  // restored; then the owner's own values over it.
  const colors = { ...(newMode === 'light' ? LIGHT_COLORS : DARK_COLORS) }
  for (const [key, value] of Object.entries(declared)) {
    if (!modeOwned(key, value)) colors[key] = value
  }
  return colors
}

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

async function _refreshThemeSwCache(css, bg, mode) {
  if (typeof caches === 'undefined' || typeof Response === 'undefined') return
  try {
    const cache = await caches.open(SHELL_DATA_CACHE)
    const url = new URL('/api/theme', self?.location?.origin || window.location.origin).href
    const body = JSON.stringify({ css, bg, mode })
    await cache.put(url, new Response(body, {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }))
  } catch {
    // Cache write failed (quota, opaque origin, no SW). Non-fatal: the
    // optimistic DOM paint already reflects the new theme.
  }
}

async function _cancelThemeQueries(queryClient) {
  await Promise.all([
    queryClient.cancelQueries?.({ queryKey: themeQueries.keys.all }),
    queryClient.cancelQueries?.({ queryKey: themeQueries.keys.mode }),
  ])
}

function _seedThemeQueries(queryClient, css, bg, mode) {
  queryClient.setQueryData?.(themeQueries.keys.all, { css, bg, mode })
  queryClient.setQueryData?.(themeQueries.keys.mode, mode)
}

/**
 * Toggle between dark and light: fetch the current CSS, swap
 * structural color vars for the opposite mode, seed the React Query
 * cache, apply to the DOM, persist to storage, then mark theme
 * queries stale without an immediate refetch. AppCanvas observes the
 * seeded cache and postMessages the new theme to live iframes.
 *
 * Returns `{ newMode, newCss, newBg }` so the caller (SettingsView)
 * can update its optimistic UI state without re-deriving them.
 *
 * Throws on persist failure so the caller can run its rollback
 * (flip lightMode back, surface an error).
 *
 * CRITICAL — cache handoff contract:
 *   `_seedThemeQueries` MUST write BOTH theme query keys. The first is
 *   what AppCanvas.jsx subscribes to (useQuery({ queryKey:
 *   themeQueryKey, ... })) and its useEffect on [theme?.css,
 *   theme?.bg] fires the `moebius:frame-theme` postMessage to live
 *   iframes. The second is what SettingsView reads to seed
 *   `lightMode` after navigation away and back. The follow-up
 *   invalidate calls intentionally use `refetchType: 'none'`: they
 *   mark the data refreshable later without letting same-tick SWR
 *   refetches repaint an older theme over the optimistic paint.
 *
 * CRITICAL — bg extraction:
 *   `newBg` is read from the BUILT CSS (`themeBg`), not from
 *   the parsed `meta.colors['--bg']` of the input. Mode swap
 *   replaces --bg, so the old meta value is stale. Passing the
 *   stale value to applyThemeToDom would leave body.background +
 *   meta theme-color pointing at the previous mode's color even
 *   though the <style> block reflects the new mode.
 */
export async function toggleTheme(queryClient, currentMode, api) {
  const newMode = currentMode === 'dark' ? 'light' : 'dark'

  // Swap relative to the css the user is CURRENTLY looking at, read
  // SYNCHRONOUSLY from the React Query cache (useTheme's /api/theme result —
  // the full css WITH @import font lines, unlike the DOM <style> whose
  // @imports applyTheme has hoisted to <link>). Reading it sync means the
  // applyThemeToDom below runs on THIS tick, so the recolor — including the
  // toggle control itself, whose track is var(--accent)/var(--border) —
  // lands immediately instead of after a storage round-trip. The old
  // `await getThemeCss()` gated the optimistic paint behind a fetch: the knob
  // slid at once but every color lagged a frame, so the control the user was
  // looking at recolored last (owner feedback).
  const cached = queryClient.getQueryData(themeQueries.keys.all)
  let currentCss = typeof cached?.css === 'string' ? cached.css : ''
  if (!currentCss) {
    // Cold cache (very early boot, before useTheme's first fetch resolved):
    // fall back to the authoritative persisted css.
    const themeRes = await api.storage.shared.getThemeCss()
    currentCss = themeRes.ok ? await themeRes.text() : ''
  }
  const meta = parseThemeMeta(currentCss)

  // Pre-toggle bg, captured from the last persisted CSS — the
  // authoritative rollback target if persistTheme below throws.
  const oldBg = themeBg(currentCss) || meta.colors['--bg']

  // Swap mode-owned colours while preserving the owner's customisations.
  const colors = colorsForMode(meta.colors, currentCss, currentMode, newMode)
  const persistedCss = buildThemeCss(colors, meta, newMode)
  // What the shell paints, seeds and hands to app frames: exactly what
  // `/api/theme` serves for the persisted css (backend `_ensure_core_vars`).
  const newCss = withThemeDefaults(persistedCss)

  // Extract NEW bg from the built CSS, not from old meta.
  const newBg = themeBg(newCss) || meta.colors['--bg']

  // Stop any in-flight /api/theme or theme-mode fetch from writing an older
  // value after the optimistic paint. Then seed both query caches BEFORE the
  // DOM apply so useTheme/AppCanvas/Settings all see the same target mode on
  // the same tick as the visual change.
  await _cancelThemeQueries(queryClient)
  _seedThemeQueries(queryClient, newCss, newBg, newMode)
  // The Settings toggle sits on the shell's back-stack sentinel, so it must not
  // use a View Transition snapshot. It also opts out of the per-element CSS
  // fallback: an atomic paint keeps borders, fills, and text in lockstep while
  // the small toggle thumb supplies the interaction motion locally.
  applyThemeToDom(newCss, newBg, newMode, { animate: false })

  try {
    // Refresh SWR's local /api/theme body before notify.send can provoke a
    // sibling loadTheme() refetch. A stale SWR body was the visible snap-back:
    // the UI painted light, then the service worker served the old dark body.
    await _refreshThemeSwCache(newCss, newBg, newMode)
    await persistTheme(persistedCss, newMode, api)
    _seedThemeQueries(queryClient, newCss, newBg, newMode)
    await _refreshThemeSwCache(newCss, newBg, newMode)
    // Mark stale so future explicit reloads know the query can refresh, but do
    // not immediately refetch: we already have the authoritative value we just
    // persisted, and another same-tick SWR refetch only adds flicker risk.
    themeQueries.invalidate(queryClient, { refetchType: 'none' })
    themeQueries.mode.invalidate(queryClient, { refetchType: 'none' })
  } catch (err) {
    _seedThemeQueries(queryClient, currentCss, oldBg, currentMode)
    await _refreshThemeSwCache(currentCss, oldBg, currentMode)
    applyThemeToDom(currentCss, oldBg, currentMode, { animate: false })
    throw err
  }

  return { newMode, newCss, newBg }
}
