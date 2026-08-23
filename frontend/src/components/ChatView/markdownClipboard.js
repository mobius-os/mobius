/** Clipboard contract for rendered assistant prose.
 *
 * Copy keeps native selection, but replaces the clipboard payload with two
 * representations of the selected assistant text:
 *   - text/markdown: source Markdown (or an equivalent serialization when the
 *     selection cuts through only part of a rendered block)
 *   - text/plain: the same selection without Markdown punctuation
 *
 * text/html is a small carrier for browsers that do not round-trip optional
 * clipboard MIME types. The composer reads the carrier but never inserts its
 * HTML. Ordinary destinations still receive readable plain text.
 */

const MARKDOWN_TYPE = 'text/markdown'
const MARKDOWN_CARRIER_ATTR = 'data-mobius-markdown'

const ELEMENT_NODE = 1
const TEXT_NODE = 3
const DOCUMENT_FRAGMENT_NODE = 11

function childrenOf(node) {
  return Array.from(node?.childNodes || [])
}

function classHas(node, name) {
  return !!node?.classList?.contains?.(name)
}

function tagOf(node) {
  return String(node?.tagName || '').toLowerCase()
}

function escapeMarkdownText(text) {
  return String(text || '')
    .replace(/\\/g, '\\\\')
    .replace(/([`*_[\]~<>#+\-!|&])/g, '\\$1')
    .replace(/(\d+)([.)])(?=\s)/g, '$1\\$2')
}

function inlineCode(text) {
  const value = String(text || '')
  const runs = value.match(/`+/g) || []
  const fence = '`'.repeat(Math.max(1, ...runs.map(run => run.length + 1)))
  const needsPadding = /^`|`$|^\s|\s$/.test(value)
  return `${fence}${needsPadding ? ' ' : ''}${value}${needsPadding ? ' ' : ''}${fence}`
}

function codeFence(text) {
  const runs = String(text || '').match(/`{3,}/g) || []
  return '`'.repeat(Math.max(3, ...runs.map(run => run.length + 1)))
}

function ignoredElement(node) {
  const tag = tagOf(node)
  return (
    tag === 'script'
    || tag === 'style'
    || tag === 'noscript'
    || tag === 'button'
    || node?.getAttribute?.('aria-hidden') === 'true'
    || classHas(node, 'md-code-lang')
    || classHas(node, 'md-code-copy')
    || classHas(node, 'chat__cursor')
    || classHas(node, 'katex-html')
  )
}

function texFrom(node) {
  const annotations = node?.querySelectorAll?.('annotation') || []
  for (const annotation of annotations) {
    if (annotation.getAttribute?.('encoding') === 'application/x-tex') {
      return annotation.textContent || ''
    }
  }
  return ''
}

function markdownChildren(node) {
  return childrenOf(node).map(markdownNode).join('')
}

function listMarkdown(node, ordered) {
  const start = ordered ? Number(node.getAttribute?.('start') || 1) : 1
  const items = childrenOf(node).filter(child => tagOf(child) === 'li')
  return items.map((item, index) => {
    const marker = ordered ? `${start + index}. ` : '- '
    const content = markdownChildren(item).trim()
    const lines = content.split('\n')
    return `${marker}${lines[0] || ''}${lines.slice(1).map(line => `\n  ${line}`).join('')}`
  }).join('\n') + '\n\n'
}

function tableMarkdown(node) {
  const rows = Array.from(node.querySelectorAll?.('tr') || [])
  if (rows.length === 0) return ''
  const cells = row => childrenOf(row)
    .filter(cell => ['th', 'td'].includes(tagOf(cell)))
    .map(cell => markdownChildren(cell).trim().replace(/\|/g, '\\|').replace(/\n/g, ' '))
  const head = cells(rows[0])
  if (head.length === 0) return ''
  const body = rows.slice(1).map(cells)
  return [
    `| ${head.join(' | ')} |`,
    `| ${head.map(() => '---').join(' | ')} |`,
    ...body.map(row => `| ${row.join(' | ')} |`),
    '',
    '',
  ].join('\n')
}

/** Serialize the selected rendered fragment back into equivalent Markdown. */
export function markdownFromFragment(node) {
  return markdownNode(node).trim()
}

function markdownNode(node) {
  if (!node) return ''
  if (node.nodeType === TEXT_NODE) return escapeMarkdownText(node.nodeValue ?? node.textContent)
  if (node.nodeType === DOCUMENT_FRAGMENT_NODE) return markdownChildren(node)
  if (node.nodeType !== ELEMENT_NODE || ignoredElement(node)) return ''

  const tag = tagOf(node)
  const childMarkdown = () => markdownChildren(node)
  const tex = (classHas(node, 'md-math-block') || classHas(node, 'katex'))
    ? texFrom(node)
    : ''
  if (tex) {
    return classHas(node, 'md-math-block') ? `$$\n${tex}\n$$\n\n` : `$${tex}$`
  }

  if (/^h[1-6]$/.test(tag)) return `${'#'.repeat(Number(tag[1]))} ${childMarkdown().trim()}\n\n`
  if (tag === 'p') return `${childMarkdown().trim()}\n\n`
  if (tag === 'strong' || tag === 'b') return `**${childMarkdown()}**`
  if (tag === 'em' || tag === 'i') return `_${childMarkdown()}_`
  if (tag === 'del' || tag === 's') return `~~${childMarkdown()}~~`
  if (tag === 'br') return '  \n'
  if (tag === 'hr') return '---\n\n'
  if (tag === 'code' && tagOf(node.parentNode) !== 'pre') return inlineCode(node.textContent)
  if (tag === 'pre') {
    const code = node.querySelector?.('code')
    const value = code?.textContent ?? node.textContent ?? ''
    const language = Array.from(code?.classList || [])
      .find(name => name.startsWith('language-'))
      ?.slice('language-'.length) || ''
    const fence = codeFence(value)
    return `${fence}${language}\n${value.replace(/\n$/, '')}\n${fence}\n\n`
  }
  if (tag === 'a') {
    const label = childMarkdown()
    const href = node.getAttribute?.('href') || ''
    if (!href) return label
    const title = node.getAttribute?.('title')
    return `[${label}](${href}${title ? ` "${title.replace(/"/g, '\\"')}"` : ''})`
  }
  if (tag === 'img') {
    const alt = node.getAttribute?.('alt') || ''
    // Rendered media URLs may carry short-lived authorization. Never copy a
    // transformed DOM URL; whole-block copies use the original source instead.
    return alt ? escapeMarkdownText(alt) : ''
  }
  if (tag === 'blockquote') {
    const value = childMarkdown().trim()
    return `${value.split('\n').map(line => `> ${line}`).join('\n')}\n\n`
  }
  if (tag === 'ul') return listMarkdown(node, false)
  if (tag === 'ol') return listMarkdown(node, true)
  if (tag === 'table') return tableMarkdown(node)

  return childMarkdown()
}

function plainChildren(node) {
  return childrenOf(node).map(plainNode).join('')
}

function plainList(node, ordered) {
  const start = ordered ? Number(node.getAttribute?.('start') || 1) : 1
  const items = childrenOf(node).filter(child => tagOf(child) === 'li')
  return items.map((item, index) => {
    const marker = ordered ? `${start + index}. ` : '- '
    const content = plainChildren(item).trim()
    return `${marker}${content.replace(/\n/g, '\n  ')}`
  }).join('\n') + '\n\n'
}

/** Serialize the same fragment as readable, formatting-free text. */
export function plainTextFromFragment(node) {
  return plainNode(node).trim()
}

function plainNode(node) {
  if (!node) return ''
  if (node.nodeType === TEXT_NODE) return String(node.nodeValue ?? node.textContent ?? '')
  if (node.nodeType === DOCUMENT_FRAGMENT_NODE) return plainChildren(node)
  if (node.nodeType !== ELEMENT_NODE || ignoredElement(node)) return ''

  const tag = tagOf(node)
  const children = () => plainChildren(node)
  const tex = (classHas(node, 'md-math-block') || classHas(node, 'katex'))
    ? texFrom(node)
    : ''
  if (tex) return classHas(node, 'md-math-block') ? `${tex}\n\n` : tex

  if (/^h[1-6]$/.test(tag) || tag === 'p') return `${children().trim()}\n\n`
  if (tag === 'br') return '\n'
  if (tag === 'hr') return '—\n\n'
  if (tag === 'pre') {
    const code = node.querySelector?.('code')
    return `${code?.textContent ?? node.textContent ?? ''}\n\n`
  }
  if (tag === 'img') return node.getAttribute?.('alt') || ''
  if (tag === 'blockquote') {
    return `${children().trim().split('\n').map(line => `> ${line}`).join('\n')}\n\n`
  }
  if (tag === 'ul') return plainList(node, false)
  if (tag === 'ol') return plainList(node, true)
  if (tag === 'table') {
    const rows = Array.from(node.querySelectorAll?.('tr') || [])
    return rows.map(row => childrenOf(row)
      .filter(cell => ['th', 'td'].includes(tagOf(cell)))
      .map(cell => plainChildren(cell).trim())
      .join('\t'))
      .join('\n') + '\n\n'
  }
  return children()
}

function rangeIntersectsNode(range, node) {
  try {
    return range.intersectsNode(node)
  } catch {
    return false
  }
}

function rangeInsideNode(range, node) {
  const nodeRange = node.ownerDocument.createRange()
  nodeRange.selectNodeContents(node)
  const overlap = nodeRange.cloneRange()

  if (node === range.startContainer || node.contains(range.startContainer)) {
    overlap.setStart(range.startContainer, range.startOffset)
  }
  if (node === range.endContainer || node.contains(range.endContainer)) {
    overlap.setEnd(range.endContainer, range.endOffset)
  }
  return overlap.collapsed ? null : overlap
}

function cloneRangeWithContext(range, block) {
  let selected = range.cloneContents()
  let ancestor = range.commonAncestorContainer
  if (ancestor.nodeType === TEXT_NODE) ancestor = ancestor.parentNode

  // Range.cloneContents() omits a common ancestor. If a selection lives
  // entirely inside one <strong>, link, list item, or code span, cloning only
  // the range would therefore lose exactly the Markdown structure the user is
  // asking to copy. Rebuild only that shallow ancestor chain; descendants stay
  // limited to the selected range.
  while (ancestor && ancestor !== block) {
    const shell = ancestor.cloneNode(false)
    shell.appendChild(selected)
    selected = shell
    ancestor = ancestor.parentNode
  }
  return selected
}

function selectedAssistantPayload(root, selection, markdownForBlock) {
  if (!root || !selection || selection.rangeCount !== 1 || selection.isCollapsed) return null
  const range = selection.getRangeAt(0)
  const blocks = Array.from(root.querySelectorAll('[data-assistant-markdown-block]'))
    .filter(block => rangeIntersectsNode(range, block))
  if (blocks.length === 0) return null

  const markdown = []
  const plain = []
  for (const block of blocks) {
    const overlap = rangeInsideNode(range, block)
    if (!overlap) continue
    const fragment = cloneRangeWithContext(overlap, block)
    const fullRange = block.ownerDocument.createRange()
    fullRange.selectNodeContents(block)
    const selectedAllRenderedText = overlap.toString() === fullRange.toString()
    const raw = selectedAllRenderedText ? markdownForBlock(block) : ''
    const markdownPart = raw?.trim() || markdownFromFragment(fragment)
    const plainPart = plainTextFromFragment(fragment)
    if (markdownPart) markdown.push(markdownPart)
    if (plainPart) plain.push(plainPart)
  }
  if (markdown.length === 0 || plain.length === 0) return null
  return { markdown: markdown.join('\n\n'), plainText: plain.join('\n\n') }
}

function escapeHtml(text) {
  return String(text || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

export function markdownClipboardHtml(markdown, plainText) {
  const encoded = encodeURIComponent(markdown)
  return `<span ${MARKDOWN_CARRIER_ATTR}="${encoded}">${escapeHtml(plainText).replace(/\n/g, '<br>')}</span>`
}

/** React onCopy handler used by the response-level copy surface. */
export function copyAssistantSelection(event, markdownForBlock) {
  const selection = event.currentTarget?.ownerDocument?.getSelection?.()
  const payload = selectedAssistantPayload(event.currentTarget, selection, markdownForBlock)
  if (!payload || !event.clipboardData) return false

  let plainWritten = false
  try { event.clipboardData.setData(MARKDOWN_TYPE, payload.markdown) } catch { /* optional */ }
  try {
    event.clipboardData.setData('text/html', markdownClipboardHtml(payload.markdown, payload.plainText))
  } catch { /* optional */ }
  try {
    event.clipboardData.setData('text/plain', payload.plainText)
    plainWritten = true
  } catch { /* native copy remains available below */ }

  if (!plainWritten) return false
  event.preventDefault()
  return true
}

function markdownFromHtmlCarrier(html) {
  const match = String(html || '').match(new RegExp(`${MARKDOWN_CARRIER_ATTR}="([^"]*)"`, 'i'))
  if (!match) return ''
  try {
    return decodeURIComponent(match[1])
  } catch {
    return ''
  }
}

/** Return null for an ordinary clipboard so native paste remains untouched. */
export function assistantClipboardText(clipboardData, preferPlainText = false) {
  if (!clipboardData) return null
  const plain = clipboardData.getData?.('text/plain') || ''
  const markdown = (
    clipboardData.getData?.(MARKDOWN_TYPE)
    || markdownFromHtmlCarrier(clipboardData.getData?.('text/html'))
  )
  if (!markdown) return null
  return preferPlainText ? plain : markdown
}

export function insertClipboardText(value, selectionStart, selectionEnd, text) {
  const current = String(value || '')
  const inserted = String(text || '')
  const start = Number.isInteger(selectionStart)
    ? Math.max(0, Math.min(selectionStart, current.length))
    : current.length
  const end = Number.isInteger(selectionEnd)
    ? Math.max(start, Math.min(selectionEnd, current.length))
    : start
  return {
    value: `${current.slice(0, start)}${inserted}${current.slice(end)}`,
    caret: start + inserted.length,
  }
}
