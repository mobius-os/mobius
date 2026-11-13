import { markdownEditorView } from './textEditor.js'

export const COMPOSER_TEXTAREA_MAX_HEIGHT = 280
export const COMPOSER_TEXTAREA_TALL_THRESHOLD = 45
let nativeSizingSupport

/**
 * How much of the chat the reader can actually SEE, in CSS pixels.
 *
 * ChatView publishes this as `--composer-room` and `.chat__input` caps its
 * growth against half of it, so the composer can never swallow the
 * conversation it belongs to.
 *
 * The pane's own height is not that number on a phone. The shell is a fixed
 * layer on an unscrollable document, so `.chat` keeps reporting its full
 * height while the soft keyboard covers the bottom half of it — only
 * `visualViewport` sees the shrink. In a tiled workspace the reverse holds:
 * the viewport is the whole window and the pane is the tighter bound. The
 * room is whichever is smaller.
 *
 * Either bound can be unknown: a retained pane is `display: none` and reports
 * clientHeight 0 until it is shown, and a non-browser runtime has no
 * visualViewport. An unknown bound must not win the `min` and collapse the
 * composer to its floor, so the other one stands alone.
 *
 * @param {object} [m]
 * @param {number} [m.paneHeight]      `.chat` clientHeight
 * @param {number} [m.viewportHeight]  `visualViewport.height`, else innerHeight
 * @returns {number} whole CSS pixels, or 0 when neither bound is known
 */
export function composerRoom({ paneHeight = 0, viewportHeight = 0 } = {}) {
  const pane = Number(paneHeight) > 0 ? Number(paneHeight) : 0
  const viewport = Number(viewportHeight) > 0 ? Number(viewportHeight) : 0
  if (!pane || !viewport) return Math.round(pane || viewport)
  return Math.round(Math.min(pane, viewport))
}

function composerPill(textarea) {
  return textarea?.closest?.('.chat__pill') || null
}

export function textareaUsesNativeSizing(css = globalThis.CSS) {
  // An injected CSS object keeps the capability boundary directly testable.
  // Cache only the real browser verdict; tests and non-browser runtimes should
  // not freeze a synthetic result for later calls.
  if (css !== globalThis.CSS) {
    return !!css?.supports?.('field-sizing', 'content')
  }
  if (nativeSizingSupport === undefined) {
    nativeSizingSupport = !!css?.supports?.('field-sizing', 'content')
  }
  return nativeSizingSupport
}

// React owns the pill's className; geometry owns this separate attribute so
// an attachment render cannot erase alignment without a textarea resize.
export function syncComposerTallState(
  textarea,
  height = textarea?.offsetHeight,
) {
  const measured = Number(height) || 0
  composerPill(textarea)?.toggleAttribute(
    'data-composer-tall',
    measured > COMPOSER_TEXTAREA_TALL_THRESHOLD,
  )
  return measured
}

/**
 * Reconcile the textarea's inline height with its current DOM value.
 *
 * Composer text changes through more than the input event: send cleanup,
 * failed-send reconciliation, voice input, restored drafts, and browser
 * foregrounding can all update or restore it. Keeping this operation shared
 * prevents an empty textarea from retaining a previous multi-line height.
 */
export function resizeComposerTextarea(textarea, value = textarea?.value) {
  const editor = markdownEditorView(textarea)
  if (editor) { editor.requestMeasure(); return 0 }
  if (!textarea?.style) return 0
  // Current browsers can own content sizing entirely in CSS. Avoid touching
  // height or reading scrollHeight on the keystroke path: that read forces a
  // document layout, whose cost grows with every mounted drawer/transcript
  // row. ResizeObserver in ChatInputBar updates the tall alignment only when
  // the browser reports an actual size change.
  if (textareaUsesNativeSizing()) return 0

  // Empty is a semantic one-line state, not a geometry question. During a
  // multi-pane mount / foreground transition Chromium can briefly report an
  // empty textarea's scrollHeight as its old or available flex height (often
  // the 280px cap). Measuring that transient value makes the blank composer
  // fill the pane until the next keystroke. Reset deterministically instead.
  if (value === '') {
    resetComposerTextarea(textarea)
    return 0
  }

  textarea.style.height = 'auto'
  const measured = Number(textarea.scrollHeight) || 0

  // Retained workspace panes can be display:none while React commits a state
  // update. Their scrollHeight is 0, which is not useful geometry; leave the
  // intrinsic one-row height in place and reconcile when the pane is visible.
  if (measured <= 0) return 0

  const height = Math.min(measured, COMPOSER_TEXTAREA_MAX_HEIGHT)
  textarea.style.height = `${height}px`
  syncComposerTallState(textarea, height)
  return height
}

/**
 * Grow an inline-editor textarea to fit its content, capped at `maxHeight`.
 *
 * For the small inline editors (the Q&A custom answer, the queued-message
 * editor) that live outside the composer pill and want to reveal a longer
 * draft instead of scrolling a cramped fixed box. Browsers with native
 * `field-sizing: content` size from CSS, so this is a no-op there; this is the
 * measurement fallback for the rest. Shared so a new inline editor reuses one
 * sizing rule rather than copying the measure/cap/overflow dance.
 */
export function autoGrowTextarea(textarea, maxHeight) {
  if (!textarea?.style || textareaUsesNativeSizing()) return
  textarea.style.height = 'auto'
  const contentHeight = Number(textarea.scrollHeight) || 0
  // A display:none pane reports 0; leave the intrinsic height rather than
  // collapsing the field to nothing.
  if (contentHeight <= 0) return
  textarea.style.height = `${Math.min(contentHeight, maxHeight)}px`
  textarea.style.overflowY = contentHeight > maxHeight ? 'auto' : 'hidden'
}

/** Collapse immediately while React is still committing an empty value. */
export function resetComposerTextarea(textarea) {
  if (markdownEditorView(textarea)) return
  if (!textarea?.style) return
  textarea.style.height = textareaUsesNativeSizing() ? '' : 'auto'
  composerPill(textarea)?.removeAttribute('data-composer-tall')
}

/**
 * Reconcile authoritative composer state with browser-owned geometry.
 *
 * Native `field-sizing` removes the per-keystroke measurement path, but it
 * cannot clear stale inline form geometry restored by Chromium. Empty remains
 * a semantic one-line state, so clear it explicitly; non-empty content can use
 * the native or measured sizing path above.
 */
export function reconcileComposerTextarea(
  textarea,
  value = textarea?.value,
) {
  const editor = markdownEditorView(textarea)
  if (editor) {
    // Foreground restoration must not retain stale empty-composer geometry.
    // The content is editor-owned; the outer host owns the height constraint.
    if (!editor.state.doc.length) {
      const host = editor.dom.closest('.chat__markdown-input')
      if (host) host.style.height = ''
      composerPill(textarea)?.removeAttribute('data-composer-tall')
    }
    editor.requestMeasure()
    return 0
  }
  if (!value) {
    resetComposerTextarea(textarea)
    return 0
  }
  return resizeComposerTextarea(textarea, value)
}

/**
 * Whether any line of the textarea's text wraps onto a second visual row.
 *
 * Compares the rendered content height with one row per source line, so it
 * needs layout: unknown geometry (no layout yet, `line-height: normal`)
 * answers true, which callers treat as the conservative case. Composer
 * history uses it to decide whether a ↑ that lands at the very start proves
 * the caret was already on the top row.
 */
export function textareaHasWrappedLines(
  textarea,
  getStyle = globalThis.getComputedStyle,
) {
  if (!textarea || typeof getStyle !== 'function') return true
  const style = getStyle(textarea)
  const lineHeight = parseFloat(style?.lineHeight)
  const padding = (parseFloat(style?.paddingTop) || 0)
    + (parseFloat(style?.paddingBottom) || 0)
  const contentHeight = Number(textarea.scrollHeight) - padding
  if (!(lineHeight > 0) || !(contentHeight > 0)) return true
  const sourceLines = String(textarea.value ?? '').split('\n').length
  return Math.round(contentHeight / lineHeight) > sourceLines
}
