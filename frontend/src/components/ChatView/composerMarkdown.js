/** Inline Markdown presentation: decorate source without changing the message or fetching its URLs. */
import { Decoration, ViewPlugin, WidgetType } from '@codemirror/view'
import { syntaxTree } from '@codemirror/language'

class ListBullet extends WidgetType {
  toDOM() {
    const span = document.createElement('span')
    span.className = 'cm-md-bullet'
    span.textContent = '•'
    span.setAttribute('aria-hidden', 'true')
    return span
  }
}

const inlineClasses = {
  StrongEmphasis: 'cm-md-strong', Emphasis: 'cm-md-emphasis',
  Strikethrough: 'cm-md-strike', InlineCode: 'cm-md-code',
  Link: 'cm-md-link', Autolink: 'cm-md-link',
}
const syntaxMarks = new Set(['EmphasisMark', 'StrikethroughMark', 'CodeMark', 'HeaderMark', 'QuoteMark', 'LinkMark'])

/** A caret inside a construct reveals its syntax; selections reveal everything they touch. */
export function markdownRangeIsActive(selection, from, to, focused) {
  return focused && selection.ranges.some(range => range.from <= to && range.to >= from)
}

export function composerMarkdownDecorations(state, { focused = true, composing = false, from = 0, to = state.doc.length } = {}) {
  const ranges = []
  const doc = state.doc
  const active = node => composing || markdownRangeIsActive(state.selection, node.from, node.to, focused)
  const mark = (node, className) => {
    if (node.to > node.from) ranges.push(Decoration.mark({ class: className }).range(node.from, node.to))
  }
  const hide = node => {
    // View-plugin replacements cannot cross line breaks. Keeping line structure
    // also keeps source navigation stable while a fence or heading is revealed.
    if (node.to > node.from && !doc.sliceString(node.from, node.to).includes('\n')) {
      ranges.push(Decoration.replace({}).range(node.from, node.to))
    }
  }
  const lineStyle = (node, className) => {
    let line = doc.lineAt(Math.max(node.from, from))
    const end = Math.min(node.to, to)
    for (;;) {
      ranges.push(Decoration.line({ class: className }).range(line.from))
      if (line.to >= end || line.number === doc.lines) break
      line = doc.line(line.number + 1)
    }
  }
  syntaxTree(state).iterate({ from, to, enter(ref) {
    const node = ref.node
    const name = node.name
    if (inlineClasses[name]) mark(node, inlineClasses[name])
    if (/^(ATX|Setext)Heading/.test(name)) lineStyle(node, `cm-md-heading cm-md-heading-${name.at(-1)}`)
    if (name === 'FencedCode' || name === 'CodeBlock') lineStyle(node, 'cm-md-code-line')
    if (name === 'Blockquote') lineStyle(node, 'cm-md-quote')
    if (name === 'TaskMarker') mark(node, 'cm-md-task')
    if (syntaxMarks.has(name)) {
      if (active(node.parent)) mark(node, 'cm-md-syntax')
      else hide(node)
    }
    if (name === 'ListMark') {
      if (active(node.parent)) mark(node, 'cm-md-syntax')
      else if (/[-+*]/.test(doc.sliceString(node.from, node.to))) {
        ranges.push(Decoration.replace({ widget: new ListBullet() }).range(node.from, node.to))
      }
    }
    if ((name === 'URL' || name === 'LinkTitle') && node.parent?.name === 'Link' && !active(node.parent)) hide(node)
    if (name === 'CodeInfo' && !active(node.parent)) hide(node)
    // Images remain source text: drafts must never load remote resources or
    // conceal an image destination that the owner has not sent yet.
    if (name === 'Image') return false
  } })
  return Decoration.set(ranges, true)
}

export const composerMarkdown = ViewPlugin.fromClass(class {
  constructor(view) { this.decorations = this.build(view) }
  build(view) {
    return composerMarkdownDecorations(view.state, {
      focused: view.hasFocus, composing: view.composing,
      from: view.viewport.from, to: view.viewport.to,
    })
  }
  update(update) {
    if (update.docChanged || update.selectionSet || update.viewportChanged || update.focusChanged
      || syntaxTree(update.startState) !== syntaxTree(update.state)) {
      this.decorations = this.build(update.view)
    }
  }
}, { decorations: instance => instance.decorations })
