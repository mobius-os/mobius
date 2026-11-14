import { Marked } from 'marked'
import { stripAugmentation } from './msgText.js'
import { isContinuationMessage, isSideQuestionMessage } from './chatRuntimeState.js'
import { renderedBlockEntries, repairInterleavedQuestionText } from './streamReducers.js'
import { markdownClipboardHtml, plainTextFromFragment } from './markdownClipboard.js'
import { markdownRangeSource } from './markdown/steerMarkdownRange.js'
import { mathTokens } from './markdown/mathTokens.js'

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

function renderedTextBlock(listElement, rowKey, block) {
  const row = Array.from(listElement?.querySelectorAll('li.chat__msg[data-key]') || [])
    .find(item => item.dataset.key === rowKey)
  return Array.from(row?.querySelectorAll(`[data-markdown-block="${block}"]`) || [])
    .find(element => element.closest('li.chat__msg') === row) || null
}

const escapeHtml = text => text.replace(/[&<>"]/g, char => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[char]))
// Math renders as the TeX annotation KaTeX carries, which is what the
// clipboard rules read from the transcript.
const texHtml = (tag, className) => token => `<${tag} class="${className}">`
  + `<annotation encoding="application/x-tex">${escapeHtml(token.text)}</annotation></${tag}>`
const markdownParser = new Marked({ gfm: true })
markdownParser.use(mathTokens(), { extensions: [
  { name: 'inlineKatex', renderer: texHtml('span', 'katex') },
  { name: 'blockKatex', renderer: texHtml('div', 'md-math-block') },
  // Raw HTML renders as nothing, as in the transcript: an HTML block is not
  // shown, and an inline tag drops while the text between tags stays.
], renderer: { html: () => '' } })

/** Markdown as plain HTML: the chat's Markdown parser and math tokens without
 *  the live renderer's effects or app context, so an image keeps its alt text
 *  and an app link stays a link. */
export function markdownHtml(text) {
  return markdownParser.parse(text)
}

/** Markdown's plain text by the transcript's clipboard rules, read from an
 *  inert template (nothing in it loads). */
function markdownPlainText(text) {
  const template = document.createElement('template')
  template.innerHTML = markdownHtml(text)
  return plainTextFromFragment(template.content)
}

/** The clipboard payload of `parts` (see `stretchProse`): the Markdown source
 *  the composer restores on paste, and readable plain text. A part rendered
 *  in `listElement` reads its plain text from the screen exactly as a text
 *  selection would (no Markdown punctuation, escapes, or private image
 *  addresses); one that is not (a stretch's start before the loaded page)
 *  reads it from its Markdown's HTML. Null when no part has plain text. */
export function stretchClipboard(parts, listElement) {
  const markdown = []
  const plain = []
  for (const part of parts) {
    let plainText = part.markdown
    if (Number.isInteger(part.block)) {
      const element = renderedTextBlock(listElement, part.key, part.block)
      plainText = element ? plainTextFromFragment(element) : markdownPlainText(part.markdown)
    }
    markdown.push(part.markdown)
    if (plainText) plain.push(plainText)
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
