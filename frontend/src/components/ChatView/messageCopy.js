// Message-to-plain-text helpers for the mobile hold-to-copy action.

import { stripAugmentation } from './msgText.js'
import { isAutoContinuationMessage } from './chatRuntimeState.js'

function questionText(block) {
  return (block.questions || []).map(question => {
    const answer = block.answers?.[question.question]
    return answer
      ? `${question.question}\n${answer}`
      : question.question
  }).filter(Boolean).join('\n\n')
}

/** Resolve the owner-visible prose in one transcript row. Tool chrome and
 * hidden prompt augmentation stay out of copied text. */
export function copyableMessageText(message) {
  if (!message || message.hidden || isAutoContinuationMessage(message)) return ''
  const parts = []
  if (Array.isArray(message.blocks) && message.blocks.length > 0) {
    for (const block of message.blocks) {
      if (block?.type === 'text' && block.content) parts.push(block.content)
      else if (block?.type === 'error' && block.message) parts.push(block.message)
      else if (block?.type === 'question') {
        const text = questionText(block)
        if (text) parts.push(text)
      }
    }
  }
  if (parts.length === 0 && typeof message.content === 'string') {
    parts.push(message.role === 'user'
      ? stripAugmentation(message.content)
      : message.content)
  }
  return parts.join('\n\n').trim()
}

/** Clipboard API first, textarea fallback for older/iOS PWA contexts. */
export async function copyPlainText(text) {
  if (!text) return false
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    try {
      const textarea = document.createElement('textarea')
      textarea.value = text
      textarea.setAttribute('readonly', '')
      textarea.style.position = 'fixed'
      textarea.style.opacity = '0'
      document.body.appendChild(textarea)
      textarea.select()
      const copied = document.execCommand('copy')
      textarea.remove()
      return copied
    } catch {
      return false
    }
  }
}
