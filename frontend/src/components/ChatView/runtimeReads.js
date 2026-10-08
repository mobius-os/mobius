/* One in-flight runtime read per chat, shared by every mounted view of it. */

const inFlightReads = new Map()

/**
 * Share the network read, not the owner. The same chat can be mounted more
 * than once (a visible surface plus a retained pane in the other world), so a
 * background refresh (poll, focus, recovery) of each copy would otherwise read
 * twice. Only background refreshes may share: a joined read can predate a
 * caller's own write, so read-after-write callers read fresh. Each view still
 * applies the result through its own generation and revision guards.
 */
export function sharedRuntimeRead(chatId, read) {
  const key = String(chatId)
  const pending = inFlightReads.get(key)
  if (pending) return pending
  const promise = Promise.resolve(read()).finally(() => {
    if (inFlightReads.get(key) === promise) inFlightReads.delete(key)
  })
  inFlightReads.set(key, promise)
  return promise
}

/** A committed invalidation makes a pre-change shared read ineligible to join. */
export function invalidateSharedRuntimeRead(chatId) {
  inFlightReads.delete(String(chatId))
}

export function resetRuntimeReadsForTests() {
  inFlightReads.clear()
}
