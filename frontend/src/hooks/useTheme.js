import { useEffect } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { apiFetch } from '../api/client.js'
import { themeQueryKey } from './queries.js'

// `bg` from /api/theme is constrained server-side to a hex color via
// the regex in theme.py:get_bg_color. We re-validate client-side so a
// future server-side change can't slip something dangerous through.
const HEX_RE = /^#[0-9a-fA-F]{3,8}$/

/**
 * Loads the effective theme from `/api/theme` (server returns the
 * user override OR the built-in default — single source of truth)
 * and applies it to the DOM.
 *
 * Extracts `@import url(...)` lines and injects them as `<link>` tags
 * so fonts load reliably. Remaining CSS goes into a single
 * `<style id="mobius-theme">` element.
 *
 * The `loadTheme` function returned for legacy callers (e.g. the
 * `theme_updated` SSE handler in Shell) invalidates the query, which
 * triggers a refetch and DOM update. Direct components could
 * `useQuery({ queryKey: ['theme'] })` instead but Shell still has
 * imperative call sites.
 */
export default function useTheme() {
  const queryClient = useQueryClient()
  const { data } = useQuery({
    queryKey: themeQueryKey,
    queryFn: async () => {
      const res = await apiFetch('/theme')
      if (!res.ok) throw new Error(`theme fetch failed: ${res.status}`)
      return res.json()
    },
    staleTime: 60_000,
  })

  useEffect(() => {
    if (!data || !data.css) return
    applyThemeToDom(data.css, data.bg)
  }, [data])

  return {
    loadTheme: () => queryClient.invalidateQueries({ queryKey: themeQueryKey }),
  }
}

function applyThemeToDom(css, bg) {
  document.querySelectorAll('link[data-theme-font]').forEach(l => l.remove())

  const imports = []
  const cssBody = css.replace(
    /@import\s+url\(\s*['"]([^'"]+)['"]\s*\)\s*;[^\S\n]*\n?/g,
    (_, url) => { imports.push(url); return '' }
  )
  imports.forEach(url => {
    const link = document.createElement('link')
    link.rel = 'stylesheet'
    link.href = url
    link.dataset.themeFont = '1'
    document.head.appendChild(link)
  })

  let el = document.getElementById('mobius-theme')
  if (!el) {
    el = document.createElement('style')
    el.id = 'mobius-theme'
  }
  // Always re-append so this style block is the LAST <head> child and
  // wins the cascade over the server-injected initial theme block.
  // appendChild on an already-attached node moves it; cheap.
  document.head.appendChild(el)
  el.textContent = cssBody

  if (bg && HEX_RE.test(bg)) {
    document.body.style.background = bg
    const meta = document.querySelector('meta[name="theme-color"]')
    if (meta) meta.setAttribute('content', bg)
  }
}
