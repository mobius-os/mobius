import { stripAugmentation } from './msgText.js'

/** What "copy this message" means: the message's prose — text blocks joined
 *  by blank lines — never tool output, thinking, question cards, or error
 *  chrome (each has its own surface). User text is stripped of hidden
 *  augmentation exactly as it renders, so the clipboard matches the screen.
 *  System rows (compaction, auto-continuation) copy nothing. */
export function messageCopyText(msg) {
  if (!msg || msg.kind === 'compaction' || msg.kind === 'auto_continuation') return ''
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
