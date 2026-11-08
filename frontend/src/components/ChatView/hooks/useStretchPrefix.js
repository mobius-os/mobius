/* The clipboard payload of the agent's latest stretch when it began before
   the loaded history page. Copy normally reads the rendered transcript; a
   stretch whose start is not loaded is read with its own requests instead
   (never paging the transcript, so the reader never moves), projected through
   the same reply presentation as the transcript, and its plain text is read
   from the Markdown's HTML by the transcript's clipboard rules. */
import { useEffect, useMemo, useState } from 'react'
import { Marked } from 'marked'
import { apiFetch, jsonOrThrow } from '../../../api/client.js'
import { assistantReplyGroups, presentAssistantActivity, presentAssistantReply } from '../assistantReplies.js'
import { replyProse, stretchTail } from '../messageCopy.js'
import { plainTextFromFragment } from '../markdownClipboard.js'
import { mathTokens } from '../markdown/mathTokens.js'

const PAGE_SIZE = 100

/** The stretch's rows before `before`, reading older pages with
 *  `fetchPage(before)` (resolving to `{ messages, offset }`) until the page
 *  holding the stretch's start. Rejects rather than return part of it. */
export async function readStretchStart(fetchPage, before) {
  const rows = []
  let cursor = before
  while (cursor > 0) {
    const page = await fetchPage(cursor)
    const tail = stretchTail(page.messages || [])
    rows.unshift(...tail.rows)
    if (tail.complete) break
    const next = page.offset || 0
    if (next >= cursor) throw new Error('Earlier messages did not advance')
    cursor = next
  }
  return rows
}

/** The Markdown of everything a stretch's rows say, as the transcript shows
 *  it: each reply projected as AssistantReply projects it, so a replayed
 *  continuation copies once even across the loaded page's edge. Read rows
 *  carry no peer-note positions, so a replay the transcript keeps split at a
 *  note copies joined: the same words, without the duplicate. */
export function stretchProse(rows) {
  return [...new Set(assistantReplyGroups(rows).values())].flatMap(group => {
    const shown = presentAssistantActivity(presentAssistantReply(group.rows)).rows
    return replyProse(shown.map(row => row.message)).map(part => part.markdown)
  })
}

/** The clipboard payload for `markdown` parts, each part's plain text from
 *  `plainTextOf(markdown)`. Null when no part has any. */
export function stretchClipboard(markdown, plainTextOf) {
  const plain = markdown.map(plainTextOf).filter(Boolean)
  if (!plain.length) return null
  return { markdown: markdown.join('\n\n'), plainText: plain.join('\n\n') }
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

/** Null while it is not needed, still loading, failed, or has no prose.
 *  `loadedRows` are the stretch's rows on the loaded page. */
export function useStretchPrefix(chatId, before, loadedRows, needed) {
  const key = needed && before > 0 ? `${chatId}:${before}` : null
  const [start, setStart] = useState({ key: null, rows: null })
  useEffect(() => {
    if (!key) return undefined
    let cancelled = false
    const fetchPage = cursor => apiFetch(`/chats/${chatId}?limit=${PAGE_SIZE}&before=${cursor}&compact=1`)
      .then(response => jsonOrThrow(response, 'Earlier messages failed to load'))
    readStretchStart(fetchPage, before)
      .then(rows => { if (!cancelled) setStart({ key, rows }) })
      .catch(() => {})
    return () => { cancelled = true }
  }, [chatId, before, key])
  const startRows = start.key === key ? start.rows : null
  return useMemo(() => (startRows && loadedRows
    ? stretchClipboard(stretchProse([...startRows, ...loadedRows]), markdownPlainText)
    : null), [startRows, loadedRows])
}
