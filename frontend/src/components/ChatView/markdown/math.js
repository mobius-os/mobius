/**
 * KaTeX rendering helpers.
 * Uses the global window.katex loaded via CDN in index.html.
 *
 * renderToString returns an HTML string for synchronous rendering
 * (no useEffect, no reflow).  Falls back to raw TeX if KaTeX is
 * not loaded or parsing fails.
 */

export function renderMathToString(tex, displayMode) {
  if (!window.katex) return null
  try {
    return window.katex.renderToString(tex, {
      displayMode,
      throwOnError: false,
    })
  } catch {
    return null
  }
}

export function renderBlockMath(tex, element) {
  if (!window.katex) {
    element.textContent = tex
    return
  }
  try {
    window.katex.render(tex, element, {
      displayMode: true,
      throwOnError: false,
    })
  } catch {
    element.textContent = tex
  }
}

export function renderInlineMath(tex, element) {
  if (!window.katex) {
    element.textContent = tex
    return
  }
  try {
    window.katex.render(tex, element, {
      displayMode: false,
      throwOnError: false,
    })
  } catch {
    element.textContent = tex
  }
}
