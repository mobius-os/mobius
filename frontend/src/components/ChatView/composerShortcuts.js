import { codeFence, inlineCodeDelimiters } from './markdownClipboard.js'

export function resolveComposerEnterAction(event, {
  hasInput = false,
  canSteer = false,
  canRequestSteer = canSteer,
  canSubmitSteer = canRequestSteer,
  canSendQueue = false,
  isTouchPrimary = false,
} = {}) {
  if (!event || event.key !== 'Enter' || event.shiftKey) return null

  const modifiedEnter = !!(event.metaKey || event.ctrlKey)
  if (!modifiedEnter && isTouchPrimary) return null

  if (hasInput) {
    if (modifiedEnter && canSubmitSteer) return 'submit-steer'
    return 'submit'
  }
  if (canRequestSteer) return 'steer'
  if (canSendQueue) return 'send-queue'
  return 'noop'
}

/** Whether an inline chat editor (such as the QA card's custom answer) should
 * treat this Enter as "send". Reuses the composer's own decision so every
 * inline editor sends on the same chord: plain Enter on desktop, Shift+Enter
 * always a newline, Enter a newline on touch, and Cmd/Ctrl+Enter always sends. These editors have no queued-steer affordance,
 * so only a real submit counts. */
export function isInlineEditorSubmit(event, { isTouchPrimary = false } = {}) {
  const action = resolveComposerEnterAction(event, { hasInput: true, isTouchPrimary })
  return action === 'submit' || action === 'submit-steer'
}

/** Paste-without-formatting chord. ClipboardEvent does not reliably retain
 * keyboard modifiers, so the composer snapshots this during keydown and lets
 * the subsequent paste event consume it. */
export function isPlainTextPasteShortcut(event) {
  return !!(
    String(event?.key || '').toLowerCase() === 'v'
    && event?.shiftKey
    && (event?.metaKey || event?.ctrlKey)
  )
}

// Chat box formatting: Cmd/Ctrl+B bold, I italic, E code. Cmd/Ctrl+K stays
// global search (lib/keyboardShortcuts.js), so links come from pasting a web
// address over selected text instead.
const FORMATTING_MARKERS = Object.freeze({ b: '**', i: '*', e: '`' })
const FORMATTING_PLACEHOLDER = 'text'

/** The Markdown marker a keydown asks for, or null. Shift and Alt chords stay
 * with the browser, nothing fires while an input method is composing, and a
 * key the shell already claimed (an owner can rebind shell shortcuts; its
 * capture listener prevents the default) stays the shell's. */
export function composerFormattingMarker(event) {
  if (!event || !(event.metaKey || event.ctrlKey) || event.shiftKey || event.altKey) return null
  if (event.isComposing || event.nativeEvent?.isComposing) return null
  if (event.defaultPrevented || event.nativeEvent?.defaultPrevented) return null
  return FORMATTING_MARKERS[String(event.key || '').toLowerCase()] ?? null
}

/** The edit that wraps the selection `value[start, end)` in `marker`.
 *
 * With nothing selected it inserts a placeholder, and the returned selection
 * covers the placeholder (or the wrapped text) so typing replaces it.
 * Whitespace at the selection's edges stays outside the markers, because
 * `** bold **` is not bold — except inside a fenced block, where the first
 * line's indentation is part of the code. Code keeps any backticks it
 * contains intact (see `codeDelimiters`). */
export function wrapComposerSelection(value, start, end, marker) {
  const current = String(value || '')
  const from = Math.max(0, Math.min(start ?? current.length, current.length))
  const to = Math.max(from, Math.min(end ?? from, current.length))
  const selected = current.slice(from, to)
  let lead = selected.match(/^\s*/)[0]
  let trail = selected.slice(lead.length).match(/\s*$/)[0]
  let inner = selected.slice(lead.length, selected.length - trail.length) || FORMATTING_PLACEHOLDER
  if (marker === '`' && inner.includes('\n')) {
    // A fenced block's first-line indentation is content: hoisting it before
    // the fence would change whitespace-significant code (`  if x:` above a
    // shallower line). Edge whitespace on the selected lines stays inside the
    // fence; only whole blank lines stay outside it.
    const keepLead = lead.slice(lead.lastIndexOf('\n') + 1)
    const trailNewline = trail.indexOf('\n')
    const keepTrail = trailNewline === -1 ? trail : trail.slice(0, trailNewline)
    inner = `${keepLead}${inner}${keepTrail}`
    lead = lead.slice(0, lead.length - keepLead.length)
    trail = trail.slice(keepTrail.length)
  }
  const { open, close } = marker === '`'
    ? codeDelimiters(inner, `${current.slice(0, from)}${lead}`, `${trail}${current.slice(to)}`)
    : { open: marker, close: marker }
  const text = `${lead}${open}${inner}${close}${trail}`
  const innerStart = from + lead.length + open.length
  return {
    text,
    value: `${current.slice(0, from)}${text}${current.slice(to)}`,
    selectionStart: innerStart,
    selectionEnd: innerStart + inner.length,
  }
}

/** Code delimiters that keep `inner` intact even when it contains backticks:
 * an inline span, or a fenced block on its own lines when it spans several
 * lines (inline code cannot). Shared with copy's Markdown serialization. */
function codeDelimiters(inner, before, after) {
  if (!inner.includes('\n')) return inlineCodeDelimiters(inner)
  const fence = codeFence(inner)
  return {
    open: `${before && !before.endsWith('\n') ? '\n' : ''}${fence}\n`,
    close: `\n${fence}${after && !after.startsWith('\n') ? '\n' : ''}`,
  }
}

const WEB_ADDRESS = /^https?:\/\/[^\s<>]+$/i

/** Pasting one web address over selected text links that text:
 * `[selection](address)`. Returns null for every other paste, which then
 * stays ordinary, including a paste with nothing selected and an address
 * pasted over another address. */
export function linkPasteText(selectedText, pastedText) {
  const address = String(pastedText || '').trim()
  const selected = String(selectedText || '')
  const label = selected.trim()
  if (!label || label.includes('\n') || !WEB_ADDRESS.test(address)) return null
  if (WEB_ADDRESS.test(label)) return null
  const lead = selected.match(/^\s*/)[0]
  const trail = selected.match(/\s*$/)[0]
  const escapedLabel = label.replace(/([\\[\]])/g, '\\$1')
  const target = /[()]/.test(address) ? `<${address}>` : address
  return `${lead}[${escapedLabel}](${target})${trail}`
}
