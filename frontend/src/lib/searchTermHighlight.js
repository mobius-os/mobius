export const SEARCH_MARK_OPEN = '\ue000'
export const SEARCH_MARK_CLOSE = '\ue001'
const CHAT_SEARCH_HIGHLIGHT_NAME = 'chat-search-result'
const MAX_HIGHLIGHT_RANGES = 128

function uniqueTerms(terms) {
  const seen = new Set()
  return (Array.isArray(terms) ? terms : [])
    .map(term => String(term || '').trim())
    .filter(term => {
      const key = term.toLocaleLowerCase()
      if (!key || seen.has(key)) return false
      seen.add(key)
      return true
    })
}

/** Parse the server's FTS sentinels once. The marked text—not a second client
 * query parser—is the exact visible match contract shared by the result row
 * and destination transcript. */
export function searchSnippetPresentation(snippet) {
  const parts = []
  const terms = []
  let marked = false
  for (const text of String(snippet || '').split(/([\ue000\ue001])/)) {
    if (text === SEARCH_MARK_OPEN) { marked = true; continue }
    if (text === SEARCH_MARK_CLOSE) { marked = false; continue }
    if (!text) continue
    parts.push({ text, marked })
    if (marked) terms.push(text)
  }
  return { parts, terms: uniqueTerms(terms) }
}

function escapedExpression(terms) {
  const alternatives = uniqueTerms(terms)
    .sort((a, b) => b.length - a.length)
    .map(term => term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
  return alternatives.length ? new RegExp(alternatives.join('|'), 'giu') : null
}

/** Pure offset form used by the DOM range builder and its focused tests. */
export function searchTextMatches(text, terms) {
  const expression = escapedExpression(terms)
  if (!expression) return []
  return [...String(text || '').matchAll(expression)].map(match => ({
    start: match.index,
    end: match.index + match[0].length,
    text: match[0],
  }))
}

/** Highlight only rendered prose without replacing React-owned text nodes.
 * The returned first Range also addresses the exact word for scroll placement.
 * Unsupported browsers still receive that Range and the caller's row pulse. */
export function highlightSearchTerms(root, terms) {
  const doc = root?.ownerDocument
  const NodeFilterCtor = doc?.defaultView?.NodeFilter
  if (!root?.querySelectorAll || !doc?.createRange || !NodeFilterCtor) {
    return { firstRange: null, clear() {} }
  }

  const ranges = []
  proseBlocks: for (const prose of root.querySelectorAll('.chat__text')) {
    const walker = doc.createTreeWalker(prose, NodeFilterCtor.SHOW_TEXT, {
      acceptNode(node) {
        if (!node.nodeValue?.trim() || node.parentElement?.closest(
          'script, style',
        )) return NodeFilterCtor.FILTER_REJECT
        return NodeFilterCtor.FILTER_ACCEPT
      },
    })
    while (walker.nextNode()) {
      const node = walker.currentNode
      for (const match of searchTextMatches(node.nodeValue, terms)) {
        const range = doc.createRange()
        range.setStart(node, match.start)
        range.setEnd(node, match.end)
        ranges.push(range)
        if (ranges.length >= MAX_HIGHLIGHT_RANGES) break proseBlocks
      }
    }
  }

  const registry = doc.defaultView?.CSS?.highlights
  const HighlightCtor = doc.defaultView?.Highlight
  const highlight = ranges.length && registry?.set && HighlightCtor
    ? new HighlightCtor(...ranges)
    : null
  if (highlight) registry.set(CHAT_SEARCH_HIGHLIGHT_NAME, highlight)

  let active = true
  return {
    firstRange: ranges[0] || null,
    clear() {
      if (!active) return
      active = false
      if (highlight && registry.get?.(CHAT_SEARCH_HIGHLIGHT_NAME) === highlight) {
        registry.delete(CHAT_SEARCH_HIGHLIGHT_NAME)
      }
    },
  }
}
