import { useEffect, useState } from 'react'

let katex = null
let katexPromise = null

function loadKatex() {
  if (katex) return Promise.resolve(katex)
  if (!katexPromise) {
    katexPromise = import('katex')
      .then(module => {
        katex = module.default || module
        return katex
      })
      .catch(error => {
        katexPromise = null
        throw error
      })
  }
  return katexPromise
}

function renderMath(renderer, tex, displayMode) {
  if (!renderer) return null
  try {
    return renderer.renderToString(tex, { displayMode, throwOnError: false })
  } catch {
    return null
  }
}

/** Loads the single KaTeX module on first mathematical content. Plain chats,
 * setup, and the shell never pay its download, parse, or resident-memory cost. */
export function useMathHtml(tex, displayMode) {
  const [rendered, setRendered] = useState(() => ({
    tex,
    displayMode,
    html: renderMath(katex, tex, displayMode),
  }))

  useEffect(() => {
    let cancelled = false
    loadKatex().then(renderer => {
      if (cancelled) return
      setRendered({
        tex,
        displayMode,
        html: renderMath(renderer, tex, displayMode),
      })
    }).catch(() => {})
    return () => { cancelled = true }
  }, [tex, displayMode])

  return rendered.tex === tex && rendered.displayMode === displayMode
    ? rendered.html
    : null
}
