/* The start of the agent's latest stretch when it began before the loaded
   history page. Copy normally reads the rendered transcript; a stretch whose
   start is not loaded reads it with its own requests instead (never paging
   the transcript, so the reader never moves). The read is lazy: it starts
   only when the footer is about to be used (the reply is hovered, focused or
   tapped), so opening a chat with a long autonomous run reads nothing extra,
   and it runs only as far back as the owner's last message. */
import { useCallback, useEffect, useState } from 'react'
import { apiFetch, jsonOrThrow } from '../../../api/client.js'
import { stretchTail } from '../messageCopy.js'
import { projectSettledSteerContinuations } from '../steerContinuity.js'

const PAGE_SIZE = 100

/** The stretch's rows before `before`, reading older pages with
 *  `fetchPage(before)` (resolving to `{ messages, offset }`) until the page
 *  holding the stretch's start. Each page is projected as the transcript
 *  projects its rows (an exact replay after a steer shows once), so its rows
 *  join the loaded ones as the transcript would show them. Rejects rather
 *  than return part of it. */
export async function readStretchStart(fetchPage, before) {
  const rows = []
  let cursor = before
  while (cursor > 0) {
    const page = await fetchPage(cursor)
    const tail = stretchTail(projectSettledSteerContinuations(page.messages || [], { preserveHidden: true }))
    rows.unshift(...tail.rows)
    if (tail.complete) break
    const next = page.offset || 0
    if (next >= cursor) throw new Error('Earlier messages did not advance')
    cursor = next
  }
  return rows
}

/** `[rows, request]`: the stretch's rows before `before` (the loaded page's
 *  first index; 0 when the stretch starts on the page), null until `request()`
 *  has been called and the read has finished. A failed read stays null and a
 *  later `request()` tries again. */
export function useStretchPrefix(chatId, before) {
  const key = before > 0 ? `${chatId}:${before}` : null
  const [wanted, setWanted] = useState(null)
  const [start, setStart] = useState({ key: null, rows: null })
  useEffect(() => {
    if (!key || wanted !== key) return undefined
    let cancelled = false
    const fetchPage = cursor => apiFetch(`/chats/${chatId}?limit=${PAGE_SIZE}&before=${cursor}&compact=1`)
      .then(response => jsonOrThrow(response, 'Earlier messages failed to load'))
    readStretchStart(fetchPage, before)
      .then(rows => { if (!cancelled) setStart({ key, rows }) })
      .catch(() => { if (!cancelled) setWanted(null) })
    return () => { cancelled = true }
  }, [chatId, before, key, wanted])
  const request = useCallback(() => { if (key) setWanted(key) }, [key])
  return [start.key === key ? start.rows : null, request]
}
