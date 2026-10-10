/**
 * Coalesce run and wait events into scoped reads of just the affected drawer
 * rows, instead of re-reading the complete chat list for every event.
 *
 * One scoped read is in flight at a time; ids that arrive meanwhile wait for
 * the next read, so two answers for the same row never land out of order.
 * Complete list reads still own first load, reconnects, and mutations:
 * - a complete read already in flight when a batch is due began before those
 *   events and would overwrite their rows, so the batch waits for it to land
 *   and then reads the rows. It never starts another complete read: the
 *   server keeps computing a read the browser cancels, so replacing a slow
 *   complete read on every event piled up concurrent list builds under load;
 * - a complete read that starts after a scoped request and lands before its
 *   answer is newer, so the scoped answer is dropped. One that fails, is
 *   cancelled, or is still in flight does not suppress it: a later landing
 *   read is newer and simply overwrites the applied rows.
 * A failed scoped read, including one over the server's id bound, becomes a
 * complete read. cancel() is final.
 */
export function createChatRowRefresh({
  readRows,
  applyRows,
  refreshAll,
  fullReadInFlight,
  fullReadMark,
  fullReadLandedSince,
  batchMs = 250,
  schedule = setTimeout,
  unschedule = clearTimeout,
}) {
  let pending = new Set()
  let timer = null
  let reading = false
  let cancelled = false

  function arm() {
    if (cancelled || reading || timer != null || pending.size === 0) return
    timer = schedule(flush, batchMs)
  }

  async function flush() {
    timer = null
    reading = true
    const ids = [...pending]
    pending = new Set()
    try {
      if (fullReadInFlight()) {
        for (const id of ids) pending.add(id)
        return
      }
      const mark = fullReadMark()
      const rows = await readRows(ids)
      if (!cancelled && !fullReadLandedSince(mark)) applyRows(ids, rows)
    } catch {
      if (!cancelled) await refreshAll()
    } finally {
      reading = false
      arm()
    }
  }

  return {
    request(chatId) {
      if (chatId == null) return
      pending.add(String(chatId))
      arm()
    },
    cancel() {
      cancelled = true
      if (timer != null) unschedule(timer)
      timer = null
    },
  }
}
