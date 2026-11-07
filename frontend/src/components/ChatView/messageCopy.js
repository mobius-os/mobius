import { stripAugmentation } from './msgText.js'
import { isContinuationMessage, isSideQuestionMessage } from './chatRuntimeState.js'
import { renderedBlockEntries, repairInterleavedQuestionText } from './streamReducers.js'
import { markdownClipboardHtml, plainTextFromFragment } from './markdownClipboard.js'
import { markdownRangeSource } from './markdown/steerMarkdownRange.js'

const isSystemRow = msg => !msg || msg.kind === 'compaction'
  || isContinuationMessage(msg) || isSideQuestionMessage(msg)

/** What "copy this message" means: the message's prose — text blocks joined
 *  by blank lines — never tool output, thinking, question cards, or error
 *  chrome (each has its own surface). User text is stripped of hidden
 *  augmentation exactly as it renders, so the clipboard matches the screen.
 *  System rows (compaction, continuation markers, side questions) copy
 *  nothing. */
export function messageCopyText(msg) {
  if (isSystemRow(msg)) return ''
  const clean = (t) => (msg.role === 'user' ? stripAugmentation(t) : t)
  if (Array.isArray(msg.blocks) && msg.blocks.length > 0) {
    return msg.blocks
      .filter((b) => b?.type === 'text' && b.content)
      .map((b) => clean(b.content))
      .filter(Boolean)
      .join('\n\n')
      .trim()
  }
  return msg.content ? clean(msg.content).trim() : ''
}

/** An open question card as copyable text: each question, then its choices
 * as a list. Answered cards and sealed secret cards never copy. */
function questionCopyText(block) {
  if (block.secure_input || block.answers) return ''
  return (block.questions || []).map(question => [
    question?.question?.trim(),
    ...(question?.options || []).map(option => option?.label && `- ${option.label}`),
  ].filter(Boolean).join('\n')).filter(Boolean).join('\n\n')
}

/** Everything a reply said, in order: each prose block (a card's closing
 *  message is prose too) and any still-open question card. Tool output,
 *  thinking, answered cards and error chrome never copy (each has its own
 *  surface), and provider text that only repeats an error is that error.
 *
 *  Each part with a `block` locates its rendered element: `message` indexes
 *  the given rows and `block` is the position MsgContent renders it at as
 *  `data-markdown-block` (renderedBlockEntries), so Copy reads the text the
 *  owner sees. A card part has only its `markdown`. */
export function replyProse(messages) {
  const rows = messages.map(msg => (msg.hidden || isSystemRow(msg) ? [] : renderedBlockEntries(
    repairInterleavedQuestionText(Array.isArray(msg.blocks) && msg.blocks.length
      ? msg.blocks : [{ type: 'text', content: msg.content, markdown_range: msg.markdown_range }]),
  )))
  const errors = new Set(rows.flat().flatMap(({ item }) => (
    item?.type === 'error' && item.message?.trim() ? [item.message.trim()] : [])))
  const parts = []
  rows.forEach((entries, message) => {
    entries.forEach(({ item: block, idx: index }) => {
      if (block?.type === 'question') {
        const markdown = questionCopyText(block)
        if (markdown) parts.push({ markdown })
      } else if (block?.type === 'text' && block.content?.trim()) {
        const markdown = (block.markdown_range
          ? markdownRangeSource(block.markdown_range) : block.content).trim()
        if (!errors.has(markdown)) parts.push({ message, block: index, markdown })
      }
    })
  })
  return parts
}

/** The rows at the end of an older history page that belong to the agent's
 *  stretch continuing past it: back to the last visible row that is not a
 *  reply. `complete` is false when the whole page belongs to the stretch, so
 *  its start lies on an earlier page still. */
export function stretchTail(messages) {
  let start = messages.length
  while (start > 0 && (messages[start - 1].hidden || messages[start - 1].role === 'assistant')) start -= 1
  return { rows: messages.slice(start), complete: start > 0 }
}

/* Each settled reply element's prose, so the one footer at the end of the
 * agent's stretch can copy every reply in it. Keyed by the rendered element,
 * so an unmounted reply drops out by itself. */
const replyProseByElement = new WeakMap()

export function registerReplyProse(replyElement, prose) {
  if (!replyElement) return
  if (prose) replyProseByElement.set(replyElement, prose)
  else replyProseByElement.delete(replyElement)
}

function renderedTextBlock(replyElement, rowKey, block) {
  const row = Array.from(replyElement?.querySelectorAll('li.chat__msg[data-key]') || [])
    .find(item => item.dataset.key === rowKey)
  return Array.from(row?.querySelectorAll(`[data-markdown-block="${block}"]`) || [])
    .find(element => element.closest('li.chat__msg') === row) || null
}

/** One reply's prose as a clipboard payload, read from the rendered reply
 *  exactly as a text selection would be: readable plain text of what is on
 *  screen (no Markdown punctuation, escapes, or private image addresses) plus
 *  the Markdown source the composer restores on paste. `rowKeys` are the keys
 *  of the rows `replyProse` read. Null when a part is not rendered. */
export function renderedReplyClipboard(replyElement, rowKeys, parts) {
  const markdown = []
  const plain = []
  for (const part of parts) {
    let plainText = part.markdown
    if (Number.isInteger(part.block)) {
      const element = renderedTextBlock(replyElement, rowKeys[part.message], part.block)
      if (!element) return null
      plainText = plainTextFromFragment(element)
    }
    markdown.push(part.markdown)
    if (plainText) plain.push(plainText)
  }
  if (plain.length === 0) return null
  return { markdown: markdown.join('\n\n'), plainText: plain.join('\n\n') }
}

/** The whole stretch's clipboard payload: `prefix`, the Markdown of any part
 *  that began before the loaded history page (copied as its source, like a
 *  card), then every reply from the one whose first row is `startKey` through
 *  `lastReply` (the reply holding the footer), in transcript order, as each
 *  registered its prose. Null when nothing in it is copyable or a part is not
 *  rendered. */
export function renderedStretchClipboard(lastReply, startKey, prefix = '') {
  const replies = Array.from(lastReply?.closest('.chat__list')?.querySelectorAll('li.chat__reply') || [])
  const end = replies.indexOf(lastReply)
  if (end < 0) return null
  const start = replies.findIndex(reply => reply.dataset.replyKey === startKey)
  const markdown = prefix ? [prefix] : []
  const plain = prefix ? [prefix] : []
  for (const reply of replies.slice(start >= 0 && start <= end ? start : end, end + 1)) {
    const prose = replyProseByElement.get(reply)
    if (!prose?.parts.length) continue
    const copied = renderedReplyClipboard(reply, prose.rowKeys, prose.parts)
    if (!copied) return null
    markdown.push(copied.markdown)
    plain.push(copied.plainText)
  }
  if (plain.length === 0) return null
  return { markdown: markdown.join('\n\n'), plainText: plain.join('\n\n') }
}

/** Write chat text to the clipboard. With Markdown, the clipboard matches a
 *  text-selection copy: readable text/plain plus the text/html carrier the
 *  composer reads back as Markdown (see markdownClipboard.js). Clipboard API
 *  first; a hidden-textarea copy covers older and iOS PWA contexts. */
export async function writeClipboard({ plainText, markdown = '' } = {}) {
  if (!plainText) return false
  const html = markdown ? markdownClipboardHtml(markdown, plainText) : ''
  try {
    if (html) {
      await navigator.clipboard.write([new ClipboardItem({
        'text/plain': new Blob([plainText], { type: 'text/plain' }),
        'text/html': new Blob([html], { type: 'text/html' }),
      })])
    } else {
      await navigator.clipboard.writeText(plainText)
    }
    return true
  } catch {
    return copyThroughDocument(plainText, html)
  }
}

function copyThroughDocument(plainText, html) {
  let textarea = null
  const writeTypes = (event) => {
    if (!html || !event.clipboardData) return
    event.clipboardData.setData('text/plain', plainText)
    event.clipboardData.setData('text/html', html)
    event.preventDefault()
  }
  try {
    textarea = document.createElement('textarea')
    textarea.value = plainText
    textarea.setAttribute('readonly', '')
    textarea.style.position = 'fixed'
    textarea.style.opacity = '0'
    document.body.appendChild(textarea)
    textarea.select()
    document.addEventListener('copy', writeTypes, true)
    return document.execCommand('copy')
  } catch {
    return false
  } finally {
    document.removeEventListener?.('copy', writeTypes, true)
    textarea?.remove()
  }
}

/** Plain text only: code blocks and tool output copy exactly as shown. */
export function copyPlainText(text) {
  return writeClipboard({ plainText: text })
}
