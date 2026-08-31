/**
 * A chat send is ambiguous when the browser loses the response: the server
 * may have committed the cid even though fetch rejected or timed out. Verify
 * reachability and replay the exact same request once. The backend's cid gate
 * turns that replay into either the original acknowledgement or a duplicate
 * acknowledgement, never a second user turn.
 */
function rememberSendReachability(error, reachability) {
  if (!error || typeof error !== 'object' || !reachability) return error
  try {
    // The composer reports this attempt's verified transport state. Reading the
    // process-wide monitor later is racy: another request may have moved it
    // between this verdict and ChatView's catch boundary.
    error.sendReachability = reachability
  } catch {
    // A browser-supplied DOMException can be non-extensible. Preserve the
    // original transport error; the presentation layer still has its shared
    // connectivity snapshot as a conservative fallback.
  }
  return error
}

export async function sendWithAmbiguityRecovery({
  send,
  verifyReachability,
  reportReachable,
  isAmbiguousError,
}) {
  let attempt = 0
  let verifiedReachability = null
  while (attempt < 2) {
    attempt += 1
    try {
      const response = await send()
      reportReachable?.()
      return response
    } catch (error) {
      if (!isAmbiguousError(error)) throw error
      if (attempt >= 2) {
        throw rememberSendReachability(error, verifiedReachability)
      }
      let reachable = false
      try {
        reachable = await verifyReachability()
      } catch {
        reachable = false
      }
      verifiedReachability = reachable ? 'online' : 'offline'
      if (!reachable) {
        throw rememberSendReachability(error, verifiedReachability)
      }
    }
  }
  throw new Error('unreachable')
}
