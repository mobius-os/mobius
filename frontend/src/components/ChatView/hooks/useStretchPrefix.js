/* The clipboard payload of the agent's latest stretch when it began before
   the loaded history page. Copy normally reads the rendered transcript; a
   stretch whose start is not loaded is read with its own requests instead
   (never paging the transcript, so the reader never moves), projected through
   the same reply presentation as the transcript, and its plain text is
   rendered with the chat's Markdown renderer, as the transcript's is. */
import { useEffect, useState } from 'react'
import { apiFetch, jsonOrThrow } from '../../../api/client.js'
import { assistantReplyGroups, presentAssistantActivity, presentAssistantReply } from '../assistantReplies.js'
import { replyProse, stretchTail } from '../messageCopy.js'
import { plainTextFromFragment } from '../markdownClipboard.js'

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
 *  continuation copies once even across the loaded page's edge. */
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

/** Reads Markdown's plain text exactly as Copy reads the transcript: rendered
 *  by the chat's Markdown renderer into an inert template (nothing loads), then
 *  serialized by the same clipboard rules. */
async function markdownPlainText() {
  const [{ createElement }, { renderToStaticMarkup }, { StandardMarkdown }] = await Promise.all([
    import('react'), import('react-dom/server'), import('../markdown/BlockRenderer.jsx'),
  ])
  return text => {
    const template = document.createElement('template')
    template.innerHTML = renderToStaticMarkup(createElement(StandardMarkdown, { text }))
    return plainTextFromFragment(template.content)
  }
}

/** Null while it is not needed, still loading, failed, or has no prose.
 *  `loadedRows` are the stretch's rows on the loaded page. */
export function useStretchPrefix(chatId, before, loadedRows, needed) {
  const key = needed && before > 0 ? `${chatId}:${before}` : null
  const [start, setStart] = useState({ key: null, rows: null })
  const [payload, setPayload] = useState({ start: null, loadedRows: null, value: null })
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
  useEffect(() => {
    if (!startRows || !loadedRows) return undefined
    let cancelled = false
    markdownPlainText()
      .then(plainTextOf => stretchClipboard(stretchProse([...startRows, ...loadedRows]), plainTextOf))
      .then(value => { if (!cancelled) setPayload({ start: startRows, loadedRows, value }) })
      .catch(() => {})
    return () => { cancelled = true }
  }, [startRows, loadedRows])
  return payload.start === startRows && payload.loadedRows === loadedRows ? payload.value : null
}
