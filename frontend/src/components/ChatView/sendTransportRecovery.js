/**
 * A chat send is ambiguous when the browser loses the response: the server
 * may have committed the cid even though fetch rejected or timed out. Verify
 * reachability and replay the exact same request once. The backend's cid gate
 * turns that replay into either the original acknowledgement or a duplicate
 * acknowledgement, never a second user turn.
 */
export async function sendWithAmbiguityRecovery({
  send,
  verifyReachability,
  reportReachable,
  isAmbiguousError,
}) {
  let attempt = 0
  while (attempt < 2) {
    attempt += 1
    try {
      const response = await send()
      reportReachable?.()
      return response
    } catch (error) {
      if (attempt >= 2 || !isAmbiguousError(error)) throw error
      let reachable = false
      try {
        reachable = await verifyReachability()
      } catch {
        reachable = false
      }
      if (!reachable) throw error
    }
  }
  throw new Error('unreachable')
}
