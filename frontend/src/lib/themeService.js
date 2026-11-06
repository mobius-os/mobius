import { themeQueries } from '../hooks/queries.js'
import { applyTheme, cascadeBg, inferMode } from './applyTheme.js'
import { captureLayoutSpace, clientPointToLayout } from './layoutSpace.js'

/**
 * Owns the shell's side of the theme: apply (DOM + body bg + meta
 * theme-color), the light/dark switch (`POST /api/theme/mode`, which the
 * server announces over SSE), and seeding/invalidating React Query.
 *
 * Why module-not-hook: applyThemeToDom is a pure side-effect helper callable
 * from anywhere — `useTheme` invokes it from an effect, and SettingsView's
 * toggleTheme orchestrates cache seed + apply + save. A hook would add
 * render-cycle coupling the helpers don't need.
 *
 * The AGENT-DRIVEN path (agent writes /data/shared/theme.css
 * directly via the storage API) goes through the SSE
 * `theme_updated` event → Shell's loadTheme → themeQueries
 * invalidate → useTheme refetches → applyThemeToDom. The local
 * settings-toggle path is faster: it paints the server's ready-made
 * preview and seeds the React Query cache before paint, so
 * Shell/AppCanvas/Settings agree before a refetch can serve stale SWR
 * data. The server alone builds theme CSS; this module only paints it.
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
let lastThemeBg = null

function signatureForTheme(css, bg, mode) {
  return JSON.stringify([css || '', bg || '', mode || inferMode(bg) || ''])
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
    lastThemeBg = bg || null
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
  // once applied; html/body paint var(--bg)), else the server's hint from the
  // last paint. It is only a hint for the frame, which reads its own cascade.
  const bg = cascadeBg(document) || lastThemeBg
  // Prefer the attribute the shell already set (authoritative).
  const mode = document.documentElement.getAttribute('data-theme')
    || inferMode(bg)
  return { css, bg, mode }
}

// The shell's service worker serves `/api/theme` StaleWhileRevalidate from
// the `mobius-shell-data` cache (see sw.js). Right after a toggle,
// `themeQueries.invalidate` triggers a refetch whose SWR response is the
// PRE-toggle (stale) theme — and `useTheme`'s apply effect then repaints
// that stale theme OVER the toggle's correct optimistic paint, snapping
// the UI back to the old mode (the "stuck on dark / reverts" symptom).
// The server just saved the authoritative new theme, so overwrite the cached
// `/api/theme` body with it: the SWR serve is now correct and the re-apply
// is a no-op instead of a regression. Best-effort — no SW / no Cache
// Storage (tests, SSR, private mode) just skips this; the optimistic paint
// + the eventual network revalidation still converge.
const SHELL_DATA_CACHE = 'mobius-shell-data'

async function _refreshThemeSwCache(theme) {
  if (typeof caches === 'undefined' || typeof Response === 'undefined') return
  try {
    const cache = await caches.open(SHELL_DATA_CACHE)
    const url = new URL('/api/theme', self?.location?.origin || window.location.origin).href
    await cache.put(url, new Response(JSON.stringify(theme), {
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

function _seedThemeQueries(queryClient, theme) {
  queryClient.setQueryData?.(themeQueries.keys.all, theme)
  queryClient.setQueryData?.(themeQueries.keys.mode, theme.mode)
}

function _paint(theme) {
  // The Settings toggle sits on the shell's back-stack sentinel, so it must not
  // use a View Transition snapshot. It also opts out of the per-element CSS
  // fallback: an atomic paint keeps borders, fills, and text in lockstep while
  // the small toggle thumb supplies the interaction motion locally.
  applyThemeToDom(theme.css, theme.bg, theme.mode, { animate: false })
}

async function _saveThemeMode(mode, api) {
  const res = await api.theme.setMode(mode)
  if (!res.ok) throw new Error(`theme save failed: ${res.status}`)
  return res.json()
}

/**
 * Toggle between dark and light. The server builds every theme: the cached
 * `/api/theme` response carries `toggle`, the theme this switch will save, so
 * it is painted on the tap, then `POST /api/theme/mode` rewrites theme.css
 * from what is on disk and returns the theme it now serves (normally the same
 * bytes, so applying it is a no-op). With no matching preview (a cold cache,
 * or a mode the server does not hold) the paint waits for that response.
 *
 * Returns `{ newMode, newCss, newBg }`. Throws on a failed save after
 * restoring the previous paint, so the caller can surface the error.
 *
 * CRITICAL — cache handoff contract:
 *   `_seedThemeQueries` MUST write BOTH theme query keys. The first is
 *   what AppCanvas.jsx subscribes to (its effect on [theme?.css, theme?.bg]
 *   posts `moebius:frame-theme` to live iframes). The second is what
 *   SettingsView reads to seed its switch after navigation. The follow-up
 *   invalidate calls use `refetchType: 'none'`: they mark the data
 *   refreshable later without letting same-tick SWR refetches repaint an
 *   older theme over the new one.
 */
export async function toggleTheme(queryClient, currentMode, api) {
  const newMode = currentMode === 'dark' ? 'light' : 'dark'
  const previous = queryClient.getQueryData(themeQueries.keys.all)
  const preview = previous?.mode === currentMode && previous.toggle?.mode === newMode
    ? previous.toggle
    : null

  // Stop any in-flight /api/theme or theme-mode fetch from writing an older
  // value after the paint, then seed both caches BEFORE the DOM apply so
  // useTheme/AppCanvas/Settings all see the target mode on the same tick.
  await _cancelThemeQueries(queryClient)
  if (preview) {
    _seedThemeQueries(queryClient, preview)
    _paint(preview)
  }

  let theme
  try {
    // Refresh SWR's local /api/theme body before the server's theme_updated
    // can provoke a sibling refetch: a stale SWR body was the visible
    // snap-back (the UI painted light, then the worker served the old dark).
    if (preview) await _refreshThemeSwCache(preview)
    theme = await _saveThemeMode(newMode, api)
  } catch (err) {
    if (preview) {
      _seedThemeQueries(queryClient, previous)
      await _refreshThemeSwCache(previous)
      _paint(previous)
    }
    throw err
  }
  // The server announced theme_updated before it answered, so this shell may
  // already be refetching /api/theme and getting the worker's older body (the
  // pre-tap theme, or the preview without its own `toggle`). Store the answer
  // in the worker first, so any later refetch gets it, then drop the
  // refetches already in flight before seeding: one landing after the seed
  // would repaint the old mode.
  await _refreshThemeSwCache(theme)
  await _cancelThemeQueries(queryClient)
  _seedThemeQueries(queryClient, theme)
  _paint(theme)
  themeQueries.invalidate(queryClient, { refetchType: 'none' })
  themeQueries.mode.invalidate(queryClient, { refetchType: 'none' })
  return { newMode: theme.mode, newCss: theme.css, newBg: theme.bg }
}
