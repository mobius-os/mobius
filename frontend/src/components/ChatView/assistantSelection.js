/**
 * useAssistantSelection — tracks whether the live text selection sits inside
 * an assistant message (long-press-drag on touch, click-drag on desktop) and
 * returns its normalized text, or null.
 *
 * Deliberately does NOT render anything near the selection. iOS/Android both
 * draw their own copy/paste callout directly over a fresh selection; a
 * "Reply" control positioned next to that rect (or pinned anywhere over the
 * transcript) either hides under it or sits somewhere that reads as a random
 * floating button. The composer already owns a stable, expected spot for
 * "things about to become part of my next message" (file chips, stacked
 * reply chips) — ReplyChips-adjacent UI in ChatInputBar renders the actual
 * "Reply to selection" affordance there instead of floating one over the
 * transcript. This hook only answers "is there one, and what does it say".
 *
 * Scoped to `[data-reply-source="assistant"]` (set by MsgContent only on
 * assistant bubbles) so selecting the owner's own sent messages never offers
 * a reply-to-quote — that surface already has copy/edit affordances.
 */
import { useEffect, useState } from 'react'
import { normalizeSelectedText } from './replyQuotes.js'

function selectedAssistantText(containerEl) {
  const selection = window.getSelection?.()
  if (!selection || selection.rangeCount === 0 || selection.isCollapsed) return null
  const text = normalizeSelectedText(selection.toString())
  if (!text) return null
  const anchor = selection.anchorNode
  const focus = selection.focusNode
  const anchorEl = anchor?.nodeType === 1 ? anchor : anchor?.parentElement
  const focusEl = focus?.nodeType === 1 ? focus : focus?.parentElement
  const source = anchorEl?.closest?.('[data-reply-source="assistant"]')
  if (!source) return null
  // Both ends of the selection must live in the same assistant bubble — a
  // drag that escapes into surrounding chrome (or another message) is not a
  // clean quote.
  if (!focusEl?.closest?.('[data-reply-source="assistant"]')) return null
  if (containerEl && !containerEl.contains(source)) return null
  return text
}

export function useAssistantSelection(containerRef) {
  const [selectedText, setSelectedText] = useState(null)

  useEffect(() => {
    function refresh() {
      setSelectedText(selectedAssistantText(containerRef?.current))
    }
    // selectionchange fires for both mouse drag-select and touch
    // long-press-drag-select — one listener covers both trigger paths.
    document.addEventListener('selectionchange', refresh)
    return () => document.removeEventListener('selectionchange', refresh)
  }, [containerRef])

  return selectedText
}
