import { useEffect, useCallback, useRef } from 'react'
import { apiFetch } from '../api/client.js'

/**
 * Loads and applies the dynamic theme CSS from storage.
 *
 * Extracts @import url(...) lines and injects them as <link> tags so fonts
 * load reliably. Remaining CSS goes into a <style> element.
 */
export default function useTheme() {
  const themeAbortRef = useRef(null)

  const loadTheme = useCallback(() => {
    // Abort any in-flight theme fetch before starting a new one so rapid
    // theme_updated events don't race and apply stale CSS last.
    themeAbortRef.current?.abort()
    themeAbortRef.current = new AbortController()
    const signal = themeAbortRef.current.signal
    apiFetch('/storage/shared/theme.css', { signal })
      .then(r => r.ok ? r.text() : null)
      .then(css => {
        if (signal.aborted) return
        let el = document.getElementById('mobius-theme')
        document.querySelectorAll('link[data-theme-font]').forEach(l => l.remove())

        if (css) {
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

          if (!el) {
            el = document.createElement('style')
            el.id = 'mobius-theme'
            document.head.appendChild(el)
          }
          el.textContent = cssBody
          const bgMatch = css.match(/--bg:\s*(#[0-9a-fA-F]{3,8})/)
          if (bgMatch) {
            document.body.style.background = bgMatch[1]
            const meta = document.querySelector('meta[name="theme-color"]')
            if (meta) meta.setAttribute('content', bgMatch[1])
          }
        } else {
          if (el) el.remove()
        }
      })
      .catch(() => {})
  }, [])

  useEffect(() => { loadTheme() }, [loadTheme])

  return { loadTheme }
}
