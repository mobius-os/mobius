/* The start of the first loaded agent stretch when it began before the loaded
   history page. Copy normally reads the rendered transcript; a stretch whose
   start is not loaded reads it with its own requests instead (never paging
   the transcript, so the reader never moves). The read is lazy: it starts
   only when the footer is about to be used (the reply is hovered, focused or
   tapped), so opening a chat with a long autonomous run reads nothing extra,
   and it runs only as far back as the owner's last message. */
import { useCallback, useEffect, useState } from 'react'
import { apiFetch, jsonOrThrow, chatPath } from '../../../api/client.js'
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
 *  has been called and the read has finished. Each page read has the same
 *  `timeoutMs` as the transcript's other history reads, and a chat switch or
 *  unmount aborts it. A failed or timed-out read stays null and the next
 *  `request()` tries again. */
export function useStretchPrefix(chatId, before, { timeoutMs, request = apiFetch } = {}) {
  const key = before > 0 ? `${chatId}:${before}` : null
  const [wanted, setWanted] = useState(null)
  const [start, setStart] = useState({ key: null, rows: null })
  const loaded = start.key === key
  useEffect(() => {
    if (!key || wanted !== key || loaded) return undefined
    const controller = new AbortController()
    const fetchPage = cursor => request(`${chatPath(chatId)}?limit=${PAGE_SIZE}&before=${cursor}&compact=1`, {
      timeoutMs, signal: controller.signal,
    }).then(response => jsonOrThrow(response, 'Earlier messages failed to load'))
    readStretchStart(fetchPage, before)
      .then(rows => { if (!controller.signal.aborted) setStart({ key, rows }) })
      .catch(() => { if (!controller.signal.aborted) setWanted(null) })
    return () => controller.abort()
  }, [chatId, before, key, wanted, loaded, timeoutMs, request])
  const requestStart = useCallback(() => { if (key) setWanted(key) }, [key])
  return [loaded ? start.rows : null, requestStart]
}
