/**
 * Word-level highlighting for a revealed search match.
 *
 * The matched terms come straight from the result snippet, where the backend
 * wrapped each matched surface form in the private-use sentinels U+E000/U+E001
 * (already produced by SQLite FTS `snippet()`). Using those exact surface forms
 * means the frontend re-does no tokenizing, prefixing, or diacritic folding —
 * whatever FTS matched is what we light up.
 *
 * Painting uses the CSS Custom Highlight API: it highlights DOM Ranges without
 * mutating the tree, so rendered markdown, KaTeX, code blocks, and links are
 * left untouched. Where the API is unavailable it degrades to nothing (the
 * message-level flash still fires), so it is purely additive.
 */

const HIGHLIGHT_NAME = 'chat-search-term'

// U+E000 … U+E001 delimit each matched surface form in a snippet.
const _SNIPPET_TERM_RE = /([\s\S]*?)/g

/** Extract the distinct matched surface forms from a result snippet. */
export function termsFromSnippet(snippet) {
  if (!snippet || typeof snippet !== 'string') return []
  const terms = new Set()
  let match
  _SNIPPET_TERM_RE.lastIndex = 0
  while ((match = _SNIPPET_TERM_RE.exec(snippet)) !== null) {
    const term = match[1].trim()
    if (term) terms.add(term)
  }
  return [...terms]
}

function _supported() {
  return (
    typeof CSS !== 'undefined'
    && !!CSS.highlights
    && typeof Highlight !== 'undefined'
    && typeof document !== 'undefined'
  )
}

// The highlight is a single document-global named highlight, but several
// ChatView instances coexist (the Shell keeps an outgoing chat mounted as an
// inert cover during a tab switch). Ownership tracking stops a departing
// instance's teardown from wiping the highlight a newer instance just painted:
// each successful paint takes a fresh token, and a scoped clear only deletes
// when it still owns it. That was the "1st chat highlights, 2nd/3rd don't"
// clobber.
let _ownerToken = 0
let _nextToken = 1

/**
 * Clear the search-term highlight.
 * - `token` omitted → force clear (any owner).
 * - `token` given → clear only if it still owns the current highlight.
 */
export function clearSearchTermHighlight(token) {
  if (token !== undefined && token !== _ownerToken) return
  _ownerToken = 0
  if (_supported()) {
    try { CSS.highlights.delete(HIGHLIGHT_NAME) } catch { /* nothing to clear */ }
  }
}

/**
 * Highlight every occurrence of `terms` inside the message row `root`,
 * case-insensitively, scoped to its rendered text (`.chat__text`).
 *
 * Returns `{ firstRange, token }` — the topmost matched Range (or null when
 * nothing matched / unsupported) so the caller can scroll that exact word into
 * view, and an ownership `token` (0 when nothing was painted) the caller passes
 * to `clearSearchTermHighlight` so only the owning instance can clear it.
 */
export function paintSearchTermHighlight(root, terms) {
  if (!_supported() || !root || !terms?.length) return { firstRange: null, token: 0 }
  const needles = terms
    .map(term => (typeof term === 'string' ? term.toLowerCase() : ''))
    .filter(Boolean)
  if (!needles.length) return { firstRange: null, token: 0 }

  const textEls = root.querySelectorAll('.chat__text')
  const scopes = textEls.length ? textEls : [root]
  const ranges = []
  for (const scope of scopes) {
    const walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT)
    let node
    while ((node = walker.nextNode())) {
      const hay = node.nodeValue.toLowerCase()
      for (const needle of needles) {
        let from = 0
        for (;;) {
          const idx = hay.indexOf(needle, from)
          if (idx === -1) break
          const range = document.createRange()
          range.setStart(node, idx)
          range.setEnd(node, idx + needle.length)
          ranges.push(range)
          from = idx + needle.length
        }
      }
    }
  }
  if (!ranges.length) return { firstRange: null, token: 0 }

  try {
    CSS.highlights.set(HIGHLIGHT_NAME, new Highlight(...ranges))
  } catch {
    return { firstRange: null, token: 0 }
  }
  _ownerToken = _nextToken++
  // The tree walker visits text nodes in document order and matches within
  // each node left-to-right, so ranges[0] is the topmost occurrence — the one
  // to bring into view.
  return { firstRange: ranges[0], token: _ownerToken }
}
