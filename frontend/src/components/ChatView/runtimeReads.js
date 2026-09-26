/* One in-flight runtime read per chat, shared by every mounted view of it. */

const inFlightReads = new Map()

/**
 * Share the network read, not the owner. The same chat can be mounted more
 * than once (a visible surface plus a retained pane in the other world), and
 * each ChatView coalesces its own read-and-apply per generation
 * (`reconcileRuntimeState`). Without this layer the hidden copy issued a second
 * read while the visible one was in flight. Each view still applies the result
 * through its own generation and revision guards.
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

export function resetRuntimeReadsForTests() {
  inFlightReads.clear()
}
