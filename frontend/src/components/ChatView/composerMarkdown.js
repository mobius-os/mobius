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

class GithubLinkMark extends WidgetType {
  toDOM() {
    const span = document.createElement('span')
    span.className = 'cm-md-github-mark'
    span.setAttribute('aria-hidden', 'true')
    return span
  }
}
const githubLinkMark = new GithubLinkMark()

function isGithubUrl(source) {
  if (!/^https?:\/\//i.test(source)) return false
  try {
    const url = new URL(source)
    // GitHub's own subdomains (www., gist.) are GitHub too; a host that only
    // starts with "github.com." is not.
    const host = url.hostname
    return (host === 'github.com' || host.endsWith('.github.com')) && !url.username && !url.password
  } catch {
    return false
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
  const lineStyles = (first, last, style) => {
    const start = Math.max(first.from, from)
    const end = Math.min(last.to, to)
    if (start > end) return
    let line = doc.lineAt(start)
    for (;;) {
      ranges.push(Decoration.line({ class: style(line) }).range(line.from))
      if (line.to >= end || line.number === doc.lines) break
      line = doc.line(line.number + 1)
    }
  }
  const lineStyle = (node, className) => lineStyles(doc.lineAt(node.from), doc.lineAt(node.to), () => className)
  const codeStyle = node => {
    const first = doc.lineAt(node.from)
    const last = doc.lineAt(node.to)
    const closingFence = node.lastChild
    // Keep fence rows in the editor so caret navigation can reveal the source.
    // Outside quotes/lists the hidden rows just provide space around the code.
    const preview = node.name === 'FencedCode' && node.parent?.name === 'Document' && !active(node)
    const bodyStart = first.number + Number(preview)
    const bodyEnd = last.number - Number(preview && closingFence?.name === 'CodeMark' && closingFence.from !== node.firstChild.from)
    lineStyles(first, last, line => {
      if (line.number < bodyStart || line.number > bodyEnd) return 'cm-md-code-fence'
      return `cm-md-code-line${line.number === bodyStart ? ' cm-md-code-start' : ''}${line.number === bodyEnd ? ' cm-md-code-end' : ''}`
    })
  }
  syntaxTree(state).iterate({ from, to, enter(ref) {
    const node = ref.node
    const name = node.name
    if (inlineClasses[name]) mark(node, inlineClasses[name])
    if (/^(ATX|Setext)Heading/.test(name)) lineStyle(node, `cm-md-heading cm-md-heading-${name.at(-1)}`)
    if (name === 'FencedCode' || name === 'CodeBlock') codeStyle(node)
    if (name === 'Blockquote') lineStyle(node, 'cm-md-quote')
    if (name === 'TaskMarker') mark(node, 'cm-md-task')
    if (syntaxMarks.has(name)) {
      if (active(node.parent)) mark(node, 'cm-md-syntax')
      else if (name === 'HeaderMark' && /^ATXHeading/.test(node.parent.name) && node.from === node.parent.from) {
        const separator = /^[\t ]*/.exec(doc.sliceString(node.to, doc.lineAt(node.to).to))[0]
        hide({ from: node.from, to: node.to + separator.length })
      } else hide(node)
    }
    if (name === 'ListMark') {
      if (active(node.parent)) mark(node, 'cm-md-syntax')
      else if (/[-+*]/.test(doc.sliceString(node.from, node.to))) {
        ranges.push(Decoration.replace({ widget: new ListBullet() }).range(node.from, node.to))
      }
    }
    if (name === 'URL') {
      const parent = node.parent
      if (parent?.name === 'LinkReference') return
      const link = parent?.name === 'Link' || parent?.name === 'Autolink' ? parent : node
      if (link === node) {
        // An autolink may stop before @, escapes, or an invalid port. Don't
        // brand a recognized prefix of an address pointing somewhere else.
        if (/[\\@%\p{L}\p{N}]/u.test(doc.sliceString(node.to, node.to + 1))) return
        mark(node, 'cm-md-link')
      }
      if (!active(link)) {
        if (parent?.name === 'Link') hide(node)
        if (isGithubUrl(doc.sliceString(node.from, node.to))) {
          ranges.push(Decoration.widget({ widget: githubLinkMark, side: -1 }).range(link.from))
        }
      }
    }
    if (name === 'LinkTitle' && node.parent?.name === 'Link' && !active(node.parent)) hide(node)
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
