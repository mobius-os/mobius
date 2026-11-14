import { stripAugmentation } from './msgText.js'
import { isContinuationMessage, isSideQuestionMessage } from './chatRuntimeState.js'
import { isAgentWorkBlock } from './activityGrouping.js'
import { bareControlName } from './toolActivityLabel.js'
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

/* Owner-facing bookkeeping that closes a turn (saving notes, asking, notifying,
 * settling a Goal). It never changes what the answer says, so prose written
 * before it is still the answer. Any other tool after the last prose means
 * that prose was a progress note ("Checking the logs:"), not an answer. */
const CLOSING_CONTROLS = new Set([
  'checkpoint_chat', 'request_question', 'request_approval', 'request_secret',
  'request_restart', 'notify_owner', 'memory_remember', 'reflection_log_friction',
  'declare_wait', 'cancel_wait', 'update_goal', 'claim_agent_work',
  'finish_agent_work', 'open_item', 'AskUserQuestion',
])

function isClosingControl(block) {
  const name = block?.tool ?? block?.name
  return block?.type === 'tool' && CLOSING_CONTROLS.has(bareControlName(name) ?? name)
}

/** Thinking and closing bookkeeping, as opposed to real work. A compacted
 *  activity block (a reloaded transcript folds adjacent tool and thinking
 *  steps into one) is bookkeeping only when every step it folded is. */
function isBookkeeping(block) {
  if (block?.type === 'thinking') return true
  if (block?.type === 'activity') {
    const entries = block.entries || []
    return entries.length > 0 && entries.every(entry => isBookkeeping(entry?.item))
  }
  return isClosingControl(block)
}

/** An ordinary question card as copyable text: each question, then its
 * choices as a list. Sealed secret cards never copy. */
function questionCopyText(block) {
  return (block.questions || []).map(question => [
    question?.question?.trim(),
    ...(question?.options || []).map(option => option?.label && `- ${option.label}`),
  ].filter(Boolean).join('\n')).filter(Boolean).join('\n\n')
}

/** What a reply's actions work from: its answer, and the failure card (an
 *  error, a usage limit, a stop) it ended on instead of finishing, if any.
 *
 *  A turn interleaves short progress notes with tool and thinking activity;
 *  the answer is the last run of prose, read across every visible row of the
 *  reply (steer-joined text appears once, on its owning row). Thinking and
 *  closing bookkeeping after the answer (a question card's own tool call, a
 *  closing save) keep it, and cards neither copy nor split the run. Real work
 *  after the last prose makes that prose a progress note: the reply then
 *  copies the question card it ended on, or nothing.
 *
 *  `parts` locate each answer block as rendered: `message` indexes the given
 *  rows and `block` is the rendered position MsgContent gives it as
 *  `data-markdown-block` (renderedBlockEntries), so Copy reads the text the
 *  owner sees.
 *
 *  A failed reply's answer is only prose still being written when it failed:
 *  a note followed by more work was narration, not an answer, and a
 *  provider's text that repeats the error message is the error itself.
 *
 *  `endingError` is the error card rendered last in the reply's last visible
 *  row, the only place a card can own recovery (`ownsRecoveryAction`). */
export function replyOutcome(messages) {
  let parts = []
  let workedSinceRun = false
  let realWorkSinceRun = false
  let endingQuestion = null
  let failed = false
  let endingError = null
  const errorMessages = new Set()
  messages.forEach((msg, message) => {
    if (msg.hidden) return
    endingError = null
    if (isSystemRow(msg)) return
    const blocks = repairInterleavedQuestionText(Array.isArray(msg.blocks) && msg.blocks.length
      ? msg.blocks : [{ type: 'text', content: msg.content, markdown_range: msg.markdown_range }])
    renderedBlockEntries(blocks).forEach(({ item: block, idx: index }) => {
      endingError = block?.type === 'error' ? block : null
      if (block?.type === 'error') {
        failed = true
        if (block.message?.trim()) errorMessages.add(block.message.trim())
      } else if (isAgentWorkBlock(block)) {
        workedSinceRun = true
        failed = false
        if (!isBookkeeping(block)) {
          realWorkSinceRun = true
          endingQuestion = null
        }
      } else if (block?.type === 'question') {
        if (!block.secure_input) endingQuestion = block
      } else if (block?.type === 'text' && block.content?.trim()) {
        if (workedSinceRun) parts = []
        workedSinceRun = false
        realWorkSinceRun = false
        endingQuestion = null
        failed = false
        parts.push({
          message,
          block: index,
          markdown: (block.markdown_range
            ? markdownRangeSource(block.markdown_range) : block.content).trim(),
        })
      }
    })
  })
  if (failed) {
    parts = workedSinceRun ? [] : parts.filter(part => !errorMessages.has(part.markdown))
  } else if (realWorkSinceRun) {
    const question = endingQuestion && questionCopyText(endingQuestion)
    parts = question ? [{ markdown: question }] : []
  }
  return { answer: parts.map(part => part.markdown).join('\n\n'), endingError, parts }
}

function renderedTextBlock(replyElement, rowKey, block) {
  const row = Array.from(replyElement?.querySelectorAll('li.chat__msg[data-key]') || [])
    .find(item => item.dataset.key === rowKey)
  return Array.from(row?.querySelectorAll(`[data-markdown-block="${block}"]`) || [])
    .find(element => element.closest('li.chat__msg') === row) || null
}

/** The reply answer's clipboard payload, read from the rendered reply exactly
 *  as a text selection would be: readable plain text of what is on screen
 *  (no Markdown punctuation, escapes, or private image addresses) plus the
 *  Markdown source the composer restores on paste. `rowKeys` are the keys of
 *  the rows `replyOutcome` read. Null when a part is not rendered. */
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
