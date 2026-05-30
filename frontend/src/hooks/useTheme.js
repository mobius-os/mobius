import { useEffect, useRef } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { themeQueries } from './queries.js'
import { applyThemeToDom } from '../lib/themeService.js'
import { api } from '../api/client.js'

/**
 * Loads the effective theme from `/api/theme` (server returns the
 * user override OR the built-in default — single source of truth)
 * and applies it to the DOM via themeService.applyThemeToDom.
 *
 * The `loadTheme` function returned for legacy callers (e.g. the
 * `theme_updated` SSE handler in Shell) invalidates the query,
 * which triggers a refetch and DOM update. Direct components
 * could `useQuery({ queryKey: ['theme'] })` instead but Shell
 * still has imperative call sites.
 *
 * THEME RECOVERY — `?reset-theme=1`. The shell honors the URL
 * parameter `?reset-theme=1` as an out-of-band recovery escape
 * hatch: when an agent ships a theme that traps the UI (full-
 * screen overlay, pointer-events: none, opaque ::before with high
 * z-index, etc.), the user can type `/?reset-theme=1` into the
 * address bar to force a reset. The handler POSTs
 * `/api/theme/reset` (which moves theme.css aside on the server,
 * preserving it as a backup), invalidates the theme query so the
 * refetch returns DEFAULT_THEME, and strips the parameter from
 * the URL via `history.replaceState` so a refresh doesn't
 * re-trigger the reset.
 */
export default function useTheme() {
  const queryClient = useQueryClient()
  const { data } = themeQueries.useQuery()
  const resetHandledRef = useRef(false)

  useEffect(() => {
    if (resetHandledRef.current) return
    if (typeof window === 'undefined') return
    const params = new URLSearchParams(window.location.search)
    if (params.get('reset-theme') !== '1') return
    resetHandledRef.current = true
    // Strip the param before the network round-trip so a refresh
    // during the POST doesn't double-reset (idempotent on the
    // server side but the user-visible URL should stop advertising
    // the recovery flag immediately).
    params.delete('reset-theme')
    const search = params.toString()
    const newUrl = window.location.pathname
      + (search ? `?${search}` : '')
      + window.location.hash
    try { window.history.replaceState(null, '', newUrl) } catch {}
    // Fire-and-forget: the server rename is the persistence step;
    // the React-Query invalidation fetches the fresh defaults.
    // Errors here are non-fatal — the user can retry by reloading
    // with the param again or visiting /recover.
    api.theme.reset()
      .catch(() => {})
      .finally(() => {
        themeQueries.invalidate(queryClient)
      })
  }, [queryClient])

  useEffect(() => {
    if (!data || !data.css) return
    applyThemeToDom(data.css, data.bg)
    // Persist the background so the cold-offline shell boot and the
    // branded offline.html can match the owner's theme before any JS
    // or network is available (read in index.html + offline.html).
    try { if (data.bg) localStorage.setItem('mobius-theme-bg', data.bg) } catch {}
  }, [data])

  return {
    loadTheme: () => themeQueries.invalidate(queryClient),
  }
}
