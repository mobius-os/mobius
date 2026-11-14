/** Text and selection operations shared by the Markdown composer and plain-text answer editors. */
import { EditorView } from '@codemirror/view'
import { isolateHistory } from '@codemirror/commands'

export function markdownEditorView(element) {
  return element?.classList?.contains('cm-content') ? EditorView.findFromDOM(element) : null
}

export function editorText(element) {
  return markdownEditorView(element)?.state.doc.toString() ?? String(element?.value ?? '')
}

export function editorSelection(element) {
  const selection = markdownEditorView(element)?.state.selection.main
  return selection
    ? { start: selection.from, end: selection.to }
    : { start: element?.selectionStart, end: element?.selectionEnd }
}

export function selectEditorRange(element, anchor, head = anchor) {
  const view = markdownEditorView(element)
  if (view) {
    const limit = view.state.doc.length
    view.dispatch({ selection: {
      anchor: Math.max(0, Math.min(anchor, limit)),
      head: Math.max(0, Math.min(head, limit)),
    } })
    return true
  }
  if (typeof element?.setSelectionRange !== 'function') return false
  try { element.setSelectionRange(anchor, head); return true } catch { return false }
}

/** Keep converted Markdown paste and formatting shortcuts in the editor's undo history. */
export function replaceMarkdownSelection(element, text) {
  const view = markdownEditorView(element)
  if (!view) return false
  view.dispatch({
    ...view.state.replaceSelection(String(text)),
    annotations: isolateHistory.of('full'),
    userEvent: 'input.paste',
    scrollIntoView: true,
  })
  return true
}
