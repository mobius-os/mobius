/* The part of the agent's latest stretch that began before the loaded history
   page, as copyable Markdown. The chat's one footer copies the whole stretch;
   this reads the missing start on its own, without paging the transcript (and
   so without moving the reader). */
import { useEffect, useState } from 'react'
import { apiFetch, jsonOrThrow } from '../../../api/client.js'
import { replyProse, stretchTail } from '../messageCopy.js'

const PAGE_SIZE = 100

/** The stretch's Markdown before `before`, reading older pages with
 *  `fetchPage(before)` (resolving to `{ messages, offset }`) until the page
 *  holding the stretch's start. Rejects rather than return part of it. */
export async function readStretchPrefix(fetchPage, before) {
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
  return replyProse(rows).map(part => part.markdown).join('\n\n')
}

/** Null while it is not needed, still loading, or failed to load. */
export function useStretchPrefix(chatId, before, needed) {
  const key = needed && before > 0 ? `${chatId}:${before}` : null
  const [loaded, setLoaded] = useState({ key: null, markdown: null })
  useEffect(() => {
    if (!key) return undefined
    let cancelled = false
    const fetchPage = cursor => apiFetch(`/chats/${chatId}?limit=${PAGE_SIZE}&before=${cursor}&compact=1`)
      .then(response => jsonOrThrow(response, 'Earlier messages failed to load'))
    readStretchPrefix(fetchPage, before)
      .then(markdown => { if (!cancelled) setLoaded({ key, markdown }) })
      .catch(() => {})
    return () => { cancelled = true }
  }, [chatId, before, key])
  return loaded.key === key ? loaded.markdown : null
}
