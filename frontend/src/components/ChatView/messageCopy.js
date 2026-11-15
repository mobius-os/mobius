import { stripAugmentation } from './msgText.js'
import { isContinuationMessage, isSideQuestionMessage } from './chatRuntimeState.js'
import { renderedBlockEntries, repairInterleavedQuestionText } from './streamReducers.js'
import { markdownClipboardHtml, plainTextFromFragment } from './markdownClipboard.js'
import { markdownRangeSource } from './markdown/steerMarkdownRange.js'
import { inlineMarkdownHtml, markdownHtml } from './markdown/markdownParser.js'
import { questionCardOpen } from './questionOpen.js'

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

/** An open question card's copy part: each question, then its choices as a
 * list, in `markdown` and as `card` (its text, for the plain text the card
 * shows). Closed cards (see questionCardOpen) and sealed secret cards never
 * copy. */
function questionCopyPart(block, liveQuestionId) {
  if (block.secure_input || !questionCardOpen(block, liveQuestionId)) return null
  const questions = (block.questions || []).map(question => ({
    question: question?.question?.trim() || '',
    options: (question?.options || []).map(option => option?.label).filter(Boolean),
  })).filter(question => question.question || question.options.length)
  const markdown = questions.map(({ question, options }) => [
    question, ...options.map(label => `- ${label}`),
  ].filter(Boolean).join('\n')).join('\n\n')
  return markdown ? { markdown, card: { questions } } : null
}

/** Everything a reply said, in order: each prose block (a card's closing
 *  message is prose too) and any still-open question card (`liveQuestionId`
 *  is the chat's open question). Tool output, thinking, closed cards and
 *  error chrome never copy (each has its own
 *  surface), and provider text that only repeats an error is that error.
 *
 *  Each part with a `block` locates its rendered element: `message` indexes
 *  the given rows and `block` is the position MsgContent renders it at as
 *  `data-markdown-block` (renderedBlockEntries), so Copy reads the text the
 *  owner sees. A card part has its `markdown` and its `card` text. */
export function replyProse(messages, { liveQuestionId = null } = {}) {
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
        const part = questionCopyPart(block, liveQuestionId)
        if (part) parts.push(part)
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

/** HTML's plain text by the transcript's clipboard rules, read from an inert
 *  template (nothing in it loads). */
function htmlPlainText(html) {
  const template = document.createElement('template')
  template.innerHTML = html
  return plainTextFromFragment(template.content)
}

/** An open card's plain text as the card shows it: each question rendered as
 *  QuestionCard renders it (tag-like text literal), then each choice as its
 *  inline label. */
function cardPlainText(card) {
  return card.questions.map(({ question, options }) => [
    question && htmlPlainText(markdownHtml(question, { literalTags: true })),
    ...options.map(label => `- ${htmlPlainText(inlineMarkdownHtml(label))}`),
  ].filter(Boolean).join('\n')).filter(Boolean).join('\n\n')
}

/** The clipboard payload of `parts` (see `stretchProse`): the Markdown source
 *  the composer restores on paste, and readable plain text. A part rendered
 *  in `listElement` reads its plain text from the screen exactly as a text
 *  selection would (no Markdown punctuation, escapes, or private image
 *  addresses); one that is not (a stretch's start before the loaded page)
 *  reads it from its Markdown's HTML, and an open card reads as the card
 *  renders it. All of them go through the chat's one Markdown parser. Null
 *  when no part has plain text. */
export function stretchClipboard(parts, listElement) {
  const markdown = []
  const plain = []
  for (const part of parts) {
    let plainText
    if (part.card) {
      plainText = cardPlainText(part.card)
    } else {
      const element = renderedTextBlock(listElement, part.key, part.block)
      plainText = element ? plainTextFromFragment(element) : htmlPlainText(markdownHtml(part.markdown))
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
