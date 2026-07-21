/**
 * Highlight.js wrapper loaded on the first code block.
 *
 * highlightSync() returns highlighted HTML if the library is already ready,
 * otherwise the CodeBlock renders plain text and upgrades after highlightCode
 * resolves. Empty chats never download the parser or language modules.
 */

let hljs = null
let ready = null

function loadHighlighter() {
  if (ready) return ready
  ready = (async () => {
    try {
      const mod = await import('highlight.js/lib/core')
      hljs = mod.default

      const langs = await Promise.all([
        import('highlight.js/lib/languages/javascript'),
        import('highlight.js/lib/languages/python'),
        import('highlight.js/lib/languages/bash'),
        import('highlight.js/lib/languages/json'),
        import('highlight.js/lib/languages/css'),
        import('highlight.js/lib/languages/xml'),
        import('highlight.js/lib/languages/typescript'),
        import('highlight.js/lib/languages/sql'),
      ])
      const names = [
        'javascript', 'python', 'bash', 'json',
        'css', 'xml', 'typescript', 'sql',
      ]
      langs.forEach((lang, i) => hljs.registerLanguage(names[i], lang.default))
      return hljs
    } catch {
      return null
    }
  })()
  return ready
}

/**
 * Synchronous highlight — returns HTML string or null.
 * Returns null if hljs hasn't loaded yet (first few hundred ms).
 */
export function highlightSync(code, language) {
  if (!hljs) return null
  try {
    if (language && hljs.getLanguage(language)) {
      return hljs.highlight(code, { language }).value
    }
    return hljs.highlightAuto(code).value
  } catch {
    return null
  }
}

/**
 * Async highlight — waits for library to load.
 * Used during streaming where we can afford to wait.
 */
export async function highlightCode(code, language) {
  const h = await loadHighlighter()
  if (!h) return null
  try {
    if (language && h.getLanguage(language)) {
      return h.highlight(code, { language }).value
    }
    return h.highlightAuto(code).value
  } catch {
    return null
  }
}
