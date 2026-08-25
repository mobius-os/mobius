/**
 * Pure helpers for stacked "reply to a quote" drafts — selected assistant
 * text + a short owner note, held in the composer until the next real send.
 */

const MAX_CHIP_QUOTE_CHARS = 80
const MAX_TOOLBAR_QUOTE_CHARS = 600

let replyIdSeq = 0

/** Stable per-draft id. Not persisted — only needs to be unique within one
 *  mounted composer's lifetime (chip keys + removal lookups). */
export function makeReplyId() {
  replyIdSeq += 1
  return `reply-${Date.now()}-${replyIdSeq}`
}

/** Collapse the whitespace a DOM selection carries across block boundaries
 *  (headings, list items, code lines) into a single readable snippet. */
export function normalizeSelectedText(raw) {
  return String(raw || '')
    .replace(/\r\n/g, '\n')
    .replace(/[ \t]+/g, ' ')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
}

/** Truncate for the compact stacked chip — quotes can run to paragraphs,
 *  the chip only needs enough to remind the owner which excerpt this is. */
export function truncateForChip(text, max = MAX_CHIP_QUOTE_CHARS) {
  const clean = normalizeSelectedText(text).replace(/\n+/g, ' ')
  if (clean.length <= max) return clean
  return `${clean.slice(0, max - 1).trimEnd()}…`
}

/** Selections can span an entire streamed answer. Cap what actually gets
 *  quoted back into the sent message — long past this, it stops reading like
 *  a quote and starts re-sending the whole reply as if the owner typed it. */
export function truncateForSend(text, max = MAX_TOOLBAR_QUOTE_CHARS) {
  const clean = normalizeSelectedText(text)
  if (clean.length <= max) return clean
  return `${clean.slice(0, max).trimEnd()}…`
}

/** One stacked reply rendered as a markdown blockquote + the owner's note,
 *  in the shape the agent should read it — not a hidden augmentation, this
 *  is plain visible message text. */
export function formatReplyForSend({ quote, note }) {
  const quotedLines = truncateForSend(quote)
    .split('\n')
    .map(line => `> ${line}`)
    .join('\n')
  const trimmedNote = String(note || '').trim()
  return `${quotedLines}\n${trimmedNote}`
}

/** All stacked replies, oldest first, ready to prepend to whatever the owner
 *  typed in the composer. Returns '' for an empty list so callers can
 *  concatenate unconditionally. */
export function formatPendingRepliesForSend(replies) {
  if (!replies?.length) return ''
  return replies.map(formatReplyForSend).join('\n\n') + '\n\n'
}
