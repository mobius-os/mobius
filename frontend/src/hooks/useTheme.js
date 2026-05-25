import { useEffect } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { themeQueries } from './queries.js'
import { applyThemeToDom } from '../lib/themeService.js'

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
 */
export default function useTheme() {
  const queryClient = useQueryClient()
  const { data } = themeQueries.useQuery()

  useEffect(() => {
    if (!data || !data.css) return
    applyThemeToDom(data.css, data.bg)
  }, [data])

  return {
    loadTheme: () => themeQueries.invalidate(queryClient),
  }
}
